"""Initialize the connector and coordinate reception, worker replies and maintenance.

Gateway events and paced REST gap repair share reception and queue services. A
separate worker drafts replies; this process validates, sends and records them.
Service responsibilities and trust boundaries are described in docs/PROTOCOL.md
and in each owning module.

Importing this module reads no configuration or token and starts no workers.
Call initialize() before using process services; main() performs startup and runs
the event loop. Configuration comes from the project root, --config PATH or
MUSE_DISCORD_CONFIG.
"""

import os
import sys
import time

try:
    import yaml  # noqa: F401 (muse_settings needs it)
    from pydantic import TypeAdapter  # noqa: F401 (Pydantic v2 required)
except ImportError:
    print("PyYAML and Pydantic v2 are required: run uv sync", file=sys.stderr)
    sys.exit(1)

from muse_discord import muse_settings as MS
from muse_discord import muse_watches as W
from muse_discord.admission import AdmissionPolicy, SharedServers
from muse_discord.bot_credentials import read_token
from muse_discord.bot_identity import BotIdentity
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
    MessagePlace,
    QueueContext,
)
from muse_discord.message_outcomes import MessageOutcomes
from muse_discord.message_queue import QueueFiles
from muse_discord.messaging import formatting as reply_formatting
from muse_discord.messaging.parts import PartDelivery
from muse_discord.messaging.sending import MessageSender
from muse_discord.messaging.typing import TypingIndicators
from muse_discord.monitor_cli import parse_options
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
from muse_discord.receiving.audit import ReceptionAudit
from muse_discord.receiving.cursor import ReceiveCursor
from muse_discord.receiving.direct import DirectChannels
from muse_discord.receiving.gateway import GatewayEvents, GatewayIngress
from muse_discord.receiving.messages import ReceivedMessages, ReceptionEffects
from muse_discord.receiving.quarantine import MessageQuarantine, is_suspicious
from muse_discord.receiving.reconciliation import MentionReconciler, RecoveryWindow
from muse_discord.receiving.staging import QueueStaging
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
from muse_discord.storage import PrivateFile, atomic_write
from muse_discord.storage import publish_private as _publish
from muse_discord.watches.audience import WatchAudience
from muse_discord.watches.delivery import WatchDelivery
from muse_discord.watches.fires import WatchFireBuilder
from muse_discord.watches.queue import WatchQueue
from muse_discord.watches.requests import WatchRequests
from muse_discord.watches.runtime import WatchRuntime, WatchState
from muse_discord.watches.web import WebWatchWork
from muse_discord.work_wait import WorkWait
from muse_discord.worker_context import WorkerContext

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
        bot_id=lambda: _IDENTITY.user_id,
        owners=lambda: _SETTINGS.owners,
        channels=lambda: _DIRECTORY.channels,
        dm_channels=lambda: _DIRECT_MESSAGES.names,
        dm_users=lambda: _DIRECT_MESSAGES.users,
        user_names=lambda: _LEDGER.names,
        write_private=lambda path, body: atomic_write(path, body, 0o600),
        ledger=lambda *args, **kwargs: _LEDGER.load(*args, **kwargs),
        api_get=lambda *args, **kwargs: api_get(*args, **kwargs),
        api_post=lambda *args, **kwargs: api_post(*args, **kwargs),
        channel_display=lambda *args, **kwargs: channel_display(*args, **kwargs),
        dm_refusal=lambda *args, **kwargs: dm_refusal(*args, **kwargs),
        load_queue=lambda: _queue_files().load(),
        never_share_hit=lambda *args, **kwargs: _REVIEW.hit(*args, **kwargs),
        normalize_parts=lambda *args, **kwargs: normalize_parts(*args, **kwargs),
        notify_owners=lambda *args, **kwargs: _OWNER_MESSAGES.notify(*args, **kwargs),
        owner_check_pending=lambda state, gid: _OWNER_PRESENCE.pending(state, gid),
        owners_absent=lambda *args, **kwargs: _OWNER_PRESENCE.absent(*args, **kwargs),
        queue_edit=lambda add=(), drop=None: _queue_files().edit(add, drop),
        queued_item_meta=lambda *args, **kwargs: _REPLIES.queued_meta(*args, **kwargs),
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
    return Conversation(_IDENTITY.user_id, _IDENTITY.name, TREAT_AS_HUMAN, frozenset(RESPOND_TO))


def is_bot_author(author):
    return _conversation().is_bot_author(author)


# The bot's own identity, fetched at startup. Used to recognize mentions,
# skip the bot's own messages, and compute its channel permissions.
_IDENTITY: BotIdentity


# Re-check for brand-new threads this often (in poll cycles). Threads are
# where forum posts live; a new post should be picked up within a few
# minutes, not at the next full channel refresh.
THREAD_REFRESH_CYCLES = 5


_DIRECTORY: DirectoryState


def _directory_refresh():
    return DirectoryRefresh(
        _DIRECTORY, _discovery(), publish_directory, _GUILD_LIFECYCLE.sync, _STATE_STORE.save
    )


def _discovery():
    return Discovery(
        api_get,
        rest_backing_off,
        lambda: _IDENTITY.user_id,
        GUILD_ALLOWLIST,
        _DIRECTORY,
        _ACCESS,
    )


def get_guilds():
    return _discovery().guilds()


# Discord permission bits used by the talk-scope computation.


def discover_channels(previous=None):
    return _discovery().channels(previous)


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
        state=_REPLIES,
        memory=_MEMORY,
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


def _queue_publication():
    return QueuePublication(_queue_files(), _STATE_STORE, _LEDGER)


def load_state():
    state = _STATE_STORE.load()
    # Restore owner admission before failed startup discovery can expose a server.
    _OWNER_PRESENCE.restore(state)
    return state


_STATE_STORE: StateFile


def _quarantine_store():
    return MessageQuarantine(QUARANTINE_FILE, _LEDGER)


_LEDGER: MessageLedger
_REPLIES: ReplyState
_RECEPTION_AUDIT: ReceptionAudit


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


_DISCOVERY_REFRESH: DiscoveryRefresh
# channel id -> its category id (from discovery), for category settings.


def scope_for(guild_id=None, channel_id=None, dm=False):
    return _DIRECTORY.scopes.for_message(guild_id, channel_id, dm)


def scope_of_channel(channel_id):
    return _DIRECTORY.scopes.for_channel(channel_id)


_SHARED_MEMBERSHIP: SharedServers


def _admission():
    return AdmissionPolicy(
        _response_policy(), _OWNER_PRESENCE, _DIRECTORY.scopes, _SHARED_MEMBERSHIP
    )


def dm_refusal(author):
    return _admission().dm_refusal(author)


_OWNER_MESSAGES: OwnerMessenger
_OWNER_PRESENCE: OwnerPresence
_REVIEW: ReplyReview
_UPDATE_NOTICES: UpdateNotices
_ADMIN_DIGESTS: AdminDigests
_JOIN_NOTICES: JoinNotices
_GUILD_LIFECYCLE: GuildLifecycle


def on_guild_event(state, ev):
    if _GUILD_LIFECYCLE.on_event(state, ev):
        _DISCOVERY_REFRESH.request()


UPDATE_CHECK_FILE = os.path.join(BASE_DIR, "update_check.json")
UPDATE_REQUEST_FILE = os.path.join(BASE_DIR, "update_request.json")


def updates_enabled():
    return bool(settings().updates_notify)


def _version_installed(tag, timeout=5):
    return version_installed(BASE_DIR, tag, timeout)


def telemetry_notice():
    """DM the owners once when usage stats have not reached their
    endpoint for days (telemetry.py, take_notice)."""
    text = TM.take_notice(settings)
    if text:
        _OWNER_MESSAGES.notify(text)


def apply_record(state, rec):
    kind = rec.get("kind")
    if kind == "update_choice":
        _UPDATE_NOTICES.apply_choice(state, rec)
    elif kind == "hold_choice":
        _REVIEW.apply_choice(state, rec)
    elif kind == "proactive_yes":
        PRO.consent.apply_button(rec)


_INTERACTIONS: InteractionRouter
_INTERACTION_RESPONDER: InteractionResponder
_INTERACTION_WORKER: InteractionWorker


def on_proactive_button(ix, rest, uid, record):
    """mdpro:yes:<token>: the Yes on the monitor's own question to one
    person (proactive.py, asks); anyone may click, only theirs counts."""
    return PRO.consent.on_button(ix, rest, uid, record) or ephemeral(
        "That button is no longer active."
    )


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
        people=_LEDGER.names,
        sessions=_SETTINGS_SESSIONS,
        outcomes=MessageOutcomes(
            _LEDGER,
            _DIRECTORY_VIEW,
            _DIRECT_MESSAGES,
            lambda: _WATCH_STORE.read()["fired"],
        ),
        admin_change=_ADMIN_DIGESTS.note,
        bot_id=str(_IDENTITY.user_id or ""),
        now=time.time(),
    )


_CAPABILITIES: CapabilityIssuer
_OWNER_AUTH: OwnerAuthorization

OWNER_DM_NONCES_FILE = os.path.join(BASE_DIR, "owner_dm_nonces.json")


def queue_index(state):
    return _queue_files().index(state, _SETTINGS.owners)


_MEMORY: OwnerMemory
MEMORY_FILE = os.environ.get("MUSE_MEMORY_FILE") or os.path.expanduser("~/MEMORY.md")


OWNER_ITEMS_FILE = os.path.join(BASE_DIR, "owner_items.json")


ITEM_NONCES_FILE = os.path.join(BASE_DIR, "item_nonces.json")


_COMMANDS: CommandRegistrar


_QUEUE_STAGING: QueueStaging
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
            _REPLIES.note_ingested,
            lambda mid: mid in _quarantine_ids() or _LEDGER.terminal(mid),
            _LEDGER.record_many,
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
        ReceptionEffects(
            watch_scan,
            TM.count,
            _RECEPTION_AUDIT.refused,
            _RECEPTION_AUDIT.no_send,
            seed_channel_seen,
        ),
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


def started_item_id(now=None):
    """The queue id of an item the monitor starts (a reminder or watch
    fire, a heartbeat slot): a snowflake for now with random low bits,
    so it passes every id check and never names a real message."""
    return W.new_fire_id(now)


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
    return IngestJournal(_LEDGER, _quarantine_store(), _REPLIES, _CAPABILITIES)


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


# Processing progress and poison-item counts belong to OutboxProcessor.
_OUTBOX: OutboxProcessor
_WORK_WAIT: WorkWait


STALE_PROMPT_FILE = os.path.join(BASE_DIR, "prompt_outdated.json")


def _reply_binding():
    return ReplyBinding(
        _CAPABILITIES.files.replies,
        lambda item: PRO.delivery.names_slot(item),
        StalePrompt(
            STALE_PROMPT_FILE, _OWNER_MESSAGES.notify, time.time, lambda: time.strftime("%Y-%m-%d")
        ),
    )


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
        _LEDGER.names,
        _OWNER_PRESENCE.absent,
        dm_refusal,
    )


def _watch_requests():
    return WatchRequests(
        _WATCH_STORE, _watch_audience(), _REPLIES.queued_meta, _note_watch_timezone
    )


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
        fires=WatchFireBuilder(audience, _LEDGER.record, _worker_context().settings_card),
        queue=WatchQueue(
            _queue_files(),
            _REPLIES,
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
        entries=lambda: _LEDGER.load()["entries"],
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


def drain_gateway(state, inbox, channels):
    return _gateway_ingress(state, channels).drain(inbox)


def sweep_mentions(state, queue, channels, dms=True):
    return MentionReconciler(
        admission=_admission(),
        ledger=_LEDGER,
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


def poll_targets():
    """The targets of one rotation. Empty until the bot knows its own id:
    nothing can be classified before that, so watermarks stay put."""
    if _IDENTITY.user_id is None:
        return []
    targets = [("dms",)] if dm_poll_user_ids() else []
    return targets + [("channel", cid) for cid in _DIRECTORY.channels]


def _drain_live_messages(state, _gateway_inbox):
    # Gateway fast path first: messages pushed in realtime are
    # classified (and triggers staged) like polled ones. Anything
    # the poll already processed is skipped. Guild join/leave
    # events ride the same inbox. Waits for the bot's identity,
    # like the poll.
    if _IDENTITY.user_id is not None:
        gw_mentions, gw_dms = drain_gateway(state, _gateway_inbox, _DIRECTORY.channels)
        if gw_mentions or gw_dms:
            print(
                f"Gateway delivered {len(gw_mentions)} mention(s), {len(gw_dms)} DM(s).",
                flush=True,
            )
        for msg, cname in gw_dms:
            stage_trigger(state, msg, cname, "dm", place=MessagePlace(guild_id=None, dm=True))


def _cleanup_cycle(state, cycle_done):
    if cycle_done:
        _JOIN_NOTICES.flush(state, _DIRECTORY.guilds, _DIRECTORY.channels)
        _UPDATE_NOTICES.announce(state)
        telemetry_notice()
        _REVIEW.expire(state)
        sweep_media()
        _generated_replies().sweep(state)
        prune_gateway_seen(state)
        _STATE_STORE.save(state)


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
                lambda: _IDENTITY.user_id is not None,
                poll_targets,
                list_dm_channels,
                check_dm_channel,
                check_channel,
                lambda: _queue_files().load(),
                sweep_mentions,
                lambda state, additions: _queue_publication().publish(state, additions),
                _STATE_STORE.save,
                lambda state: _directory_refresh().channels(state),
                lambda state: _directory_refresh().threads(state),
            ),
        ),
        _DISCOVERY_REFRESH,
    )


def _settings_upkeep(status):
    _COMMANDS.register(status)
    _ADMIN_DIGESTS.flush()


def _send_outbox(state):
    _OUTBOX.pass_once(state)
    _STATE_STORE.save(state)


def _start_monitor_threads(state):
    _gateway_inbox, _gateway_status = _GATEWAY.start()
    _ix_inbox = _GATEWAY.interactions
    _TYPING.start()
    # Restore approval mirrors before accepting clicks on the dedicated worker.
    _REVIEW.restore(state)
    _UPDATE_NOTICES.restore(state)
    _INTERACTION_WORKER.start(_ix_inbox, lambda rec: _gateway_inbox.put({"_record": rec}))

    return _gateway_inbox, _gateway_status


def _start_monitor():
    _PROCESS_LOCK.acquire()
    TM.start()
    touch_heartbeat()
    # The watchdog waits for identity before slow channel discovery.
    _IDENTITY.start()
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
    _GUILD_LIFECYCLE.sync(state, _DIRECTORY.guilds, _DIRECTORY.channels)

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

    _QUEUE_STAGING.recover(state)
    _STATE_STORE.save(state)

    return state, inbox, status


def _initialize_owner_services():
    """Connect owner authorization, private capabilities, notices and reply review."""
    global _MEMORY, _CAPABILITIES, _OWNER_AUTH
    global _OWNER_MESSAGES, _OWNER_PRESENCE, _REVIEW, _UPDATE_NOTICES
    global _ADMIN_DIGESTS, _JOIN_NOTICES, _GUILD_LIFECYCLE
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
    _MEMORY = OwnerMemory(
        lambda: MEMORY_FILE,
        _OWNER_AUTH,
        lambda: bool(settings().top("memory.save_dms")),
        lambda text: secret_in(text),
    )
    _CAPABILITIES = CapabilityIssuer(
        CapabilityFiles(
            PrivateFile(lambda: OWNER_DM_NONCES_FILE),
            PrivateFile(lambda: OWNER_ITEMS_FILE),
            PrivateFile(lambda: ITEM_NONCES_FILE),
        ),
        _OWNER_AUTH,
        CommandTargets(_DIRECTORY.channels, _DIRECTORY.guild_channels),
        lambda: _queue_files().load(),
        lambda text: _OWNER_MESSAGES.notify(text),
    )
    _REVIEW = ReplyReview(settings, _OWNER_MESSAGES, _DIRECT_MESSAGES.users, _REPLIES.settle)
    _UPDATE_NOTICES = UpdateNotices(
        lambda: UPDATE_CHECK_FILE,
        lambda: UPDATE_REQUEST_FILE,
        _OWNER_MESSAGES,
        updates_enabled,
        lambda: rest_backing_off(),
        lambda *args, **kwargs: _version_installed(*args, **kwargs),
    )
    _ADMIN_DIGESTS = AdminDigests(
        _DIRECTORY_VIEW.name,
        _LEDGER.names,
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


def _initialize_services():
    """Build process-owned services after configuration and token validation."""
    global _REPLIES, _QUEUE_STAGING, _LEDGER, _RECEPTION_AUDIT
    global _IDENTITY, _OUTBOX, _WORK_WAIT
    global _SENDER, _PARTS, _TYPING
    global _PROCESS_LOCK, _GATEWAY, _ACCESS, _AUTHOR_ROLES, _WATCH_STORE, _WATCH_STATE
    global _DIRECTORY, TM, PRO, _DIRECTORY_VIEW, _ACTIVITY, _DISCOVERY_REFRESH, _SHARED_MEMBERSHIP
    global _DIRECT_MESSAGES, _DISCORD, _BACKOFF, _HEALTH, _OUTPUT, _STATE_STORE
    global _SETTINGS_SESSIONS, _COMMANDS, _INTERACTIONS, _INTERACTION_RESPONDER, _INTERACTION_WORKER
    _IDENTITY = BotIdentity(
        lambda path: api_get(path),
        lambda: _BACKOFF.clean_cycle(),
        lambda where: token_rejected_exit(where),
    )
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
        lambda text: _OWNER_MESSAGES.notify(text),
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
        lambda: _queue_files().load(),
        lambda: time.time(),
        lambda seconds: time.sleep(seconds),
    )
    _PARTS = PartDelivery(_SENDER, lambda cid: _TYPING.send(cid), lambda secs: time.sleep(secs))
    _DIRECTORY_VIEW = DirectorySnapshot(lambda: DIRECTORY_FILE)
    _DIRECT_MESSAGES = DirectChannels()
    _LEDGER = MessageLedger(
        lambda: LEDGER_FILE,
        _DIRECTORY.channels,
        _DIRECT_MESSAGES,
        lambda *args: TM.ledger(*args),
        now=lambda: time.time(),
    )
    _REPLIES = ReplyState(
        _LEDGER,
        lambda: _queue_files().load(),
        lambda mid, meta: PRO.policy.note_item(mid, meta),
        lambda seconds: TM.latency(seconds),
        lambda: time.time(),
    )
    _RECEPTION_AUDIT = ReceptionAudit(_LEDGER)
    _QUEUE_STAGING = QueueStaging(_bursts, _queue_publication)
    _initialize_owner_services()
    _OUTBOX = OutboxProcessor(
        _outbox_files,
        lambda state: queue_index(state),
        _STATE_STORE.save,
        _CAPABILITIES.publish,
        _deliver_outbox_item,
    )
    _WORK_WAIT = WorkWait(
        lambda: time.time(),
        lambda state, now: pending_wait(state, now),
        lambda since: _OUTBOX.waiting(since),
        lambda: rest_backing_off(),
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
        lambda gid: _OWNER_PRESENCE.absent(gid),
        lambda: time.time(),
    )
    _COMMANDS = CommandRegistrar(
        get=lambda path: api_get(path),
        post=lambda path, data: api_post(path, data),
        patch=lambda path, data: api_patch(path, data),
        identity_ready=lambda: _IDENTITY.user_id is not None,
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
        _SETTINGS = LiveSettings(
            MS.Watcher(
                config_path
                or os.environ.get("MUSE_DISCORD_CONFIG")
                or os.path.join(BASE_DIR, "config.yaml")
            )
        )
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
    TOKEN = (
        token_override
        if token_override is not None
        else (read_token(initial.token_sources(), BASE_DIR) or "")
    )
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
        options = parse_options()
        initialize(options.config, token_override=options.token)
    state, inbox, status = _start_monitor()
    scheduler = _rest_scheduler()
    outbox_pass_at = 0.0
    while True:
        if _HEALTH.rejected or status.get("token_rejected"):
            _STATE_STORE.save(state)
            token_rejected_exit(_HEALTH.rejected or "gateway closed with code 4004")
        touch_heartbeat()
        _IDENTITY.from_gateway(status)
        now, backing = time.time(), rest_backing_off()
        scheduler.begin_tick(now, backing)
        dm_items = []
        _run_phase("Gateway drain", _drain_live_messages, state, inbox)
        _run_phase(
            "Settings overlay", _SETTINGS.retire_changes, _OWNER_MESSAGES.notify, rest_backing_off
        )
        _run_phase("/settings upkeep", _settings_upkeep, status)
        _run_phase("Owner DM upkeep", _CAPABILITIES.upkeep, state)
        _run_phase("Reminders and watches", watch_tick, state)
        _run_phase("Proactive", PRO.tick, state)
        poll_turn, rest_spent = scheduler.poll_slice(state, now, backing, dm_items)
        queue = _run_phase("Queue/staging", _QUEUE_STAGING.stage, state, dm_items)
        _run_phase("Typing", _TYPING.refresh, state, queue)
        outbox_pass_at = time.time()
        _run_phase("Outbox", _send_outbox, state)
        recovery = scheduler.recovery_cycle()
        _run_phase("Identity retry", _IDENTITY.retry, scheduler.cycle_done)
        _run_phase("Job scheduling", scheduler.schedule_jobs, state, recovery)
        scheduler.run_job(state, poll_turn, rest_spent, queue is not None)
        _run_phase("Cleanup", _cleanup_cycle, state, scheduler.cycle_done)
        TM.tick(settings, _DIRECTORY.guilds, _DIRECTORY.member_counts)
        _WORK_WAIT.wait(state, status["wake"], scheduler.next_wait, outbox_pass_at)


if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        TM.crashed(e)  # its class name only; SystemExit is not a crash
        raise
