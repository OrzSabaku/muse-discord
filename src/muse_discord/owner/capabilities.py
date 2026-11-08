"""Issue, publish and recover capabilities for verified worker queue items.

Owner-DM, owner-command and reply capabilities stay in monitor-owned state and
private files. The queue never receives their secrets. Authorization and final
consumption are separate responsibilities; issuance alone spends nothing.
"""

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.message_models import PROACTIVE_KIND, OwnerCommand
from muse_discord.message_queue import QueueFiles, keep_last, queued_owner_dm
from muse_discord.owner.authorization import OwnerAuthorization
from muse_discord.owner.targets import CommandTargets
from muse_discord.storage import PrivateFile

OWNER_DM_NONCE_MAX_AGE = 24 * 3600
OWNER_DM_UPKEEP_SECS = 15
OWNER_ITEM_GRACE_SECS = 120


@dataclass
class CapabilityFiles:
    owner_dms: PrivateFile
    commands: PrivateFile
    replies: PrivateFile


@dataclass
class CapabilityIssuer:
    files: CapabilityFiles
    auth: OwnerAuthorization
    targets: CommandTargets
    queue: QueueFiles
    notify: Callable[[str], object]
    upkeep_clock: dict = field(default_factory=lambda: {"next": 0.0})
    reply_alert: dict = field(default_factory=lambda: {"day": None})

    def mint_owner_dm(self, state, mid, channel_id=None, fragments=()):
        """A capability for one owner-DM queue item (see owner_dm_gate and
        its wake): 128 random bits, kept in state and the hook's private
        file, never in reply_queue.json. channel_id and fragments (its burst
        fragments' ids): so the gate knows the DM's channel and fragments
        after item_meta and reply_aliases (both bounded, fed by everyone's
        messages) have moved on."""
        n = state.setdefault("nl_nonces", {})
        n[str(mid)] = {"nonce": secrets.token_hex(16), "at": time.time()}
        if channel_id:
            n[str(mid)]["channel_id"] = str(channel_id)
        if fragments:
            n[str(mid)]["fragments"] = [str(f) for f in fragments]
        keep_last(n, 300)
        self.write_owner_dms(state)

    def write_owner_dms(self, state):
        """owner_dm_nonces.json = the nonces of open owner-DM items (written
        only when that changed; expired ones dropped)."""
        n = state.get("nl_nonces") or {}
        now = time.time()
        for k in [
            k
            for k, v in n.items()
            if not isinstance(v, dict) or now - v.get("at", 0) > OWNER_DM_NONCE_MAX_AGE
        ]:
            del n[k]
        self.write_commands(state)  # an owner DM's command uses its nonce
        self.files.owner_dms.publish({k: v["nonce"] for k, v in n.items()})

    def upkeep(self, state, force=False):
        """Main loop (every OWNER_DM_UPKEEP_SECS): every owner DM still in
        the queue holds a live nonce. Kept fresh while it is queued (the
        24-hour prune is for spent ones), and minted again when it is
        missing (state.json lost, or an item queued before nonces existed),
        so an owner DM never sits unanswerable: its next owner-DM-only wake
        carries the new one."""
        now = time.time()
        if not force and now < self.upkeep_clock["next"]:
            return
        self.upkeep_clock["next"] = now + OWNER_DM_UPKEEP_SECS
        try:
            queue = self.queue.load()
        except OSError, ValueError:
            return
        n = state.setdefault("nl_nonces", {})
        for q in queue:
            if (
                not isinstance(q, dict)
                or not q.get("message_id")
                or not queued_owner_dm(state, q, self.auth.owners)
            ):
                continue
            mid = str(q["message_id"])
            frags = [
                str(f["message_id"])
                for f in q.get("burst_fragments") or []
                if isinstance(f, dict) and f.get("message_id")
            ]
            e = n.get(mid)
            if isinstance(e, dict) and e.get("nonce"):
                e["at"] = now
                if not e.get("channel_id") and q.get("channel_id"):
                    e["channel_id"] = str(q["channel_id"])
                if frags and not e.get("fragments"):
                    e["fragments"] = frags
                continue
            n[mid] = {
                "nonce": secrets.token_hex(16),
                "at": now,
                "channel_id": str(q.get("channel_id") or ""),
            }
            if frags:
                n[mid]["fragments"] = frags
            print(f"Owner DM {mid} had no nonce; minted one for its next wake.", flush=True)
        self.upkeep_commands(state, queue, now)
        self.write_owner_dms(state)

    def note_command(self, state, mid, dm=False, command=None):
        """Queue item mid is an owner's command: mint its nonce (an owner
        DM's is its owner_dm_nonce), resolve where the owner's own words
        point (owner_targets, ids only), and write owner_items.json."""
        command = command or OwnerCommand()
        channel_id = command.channel_id
        content = command.content
        guild_id = command.guild_id
        e = state.setdefault("owner_items", {})
        ids, post = self.targets.resolve(content, guild_id)
        e[str(mid)] = {
            "at": time.time(),
            "channel_id": str(channel_id or ""),
            "dm": bool(dm),
            "channels": ids,
            "post_channels": post,
        }
        if not dm:
            e[str(mid)]["nonce"] = secrets.token_hex(16)
        keep_last(e, 300)
        self.write_commands(state)

    def write_commands(self, state, check=False):
        """owner_items.json = {message_id: {nonce, channel_id, dm, channels,
        post_channels}} for the open owner-command items (written only when that
        changed; check: also when the file was lost or mangled)."""
        nl = state.get("nl_nonces") or {}
        body = {}
        for mid, e in (state.get("owner_items") or {}).items():
            if not isinstance(e, dict):
                continue
            n = e.get("nonce") or (nl.get(mid) or {}).get("nonce")
            if n:
                body[mid] = {
                    "nonce": n,
                    "channel_id": e.get("channel_id"),
                    "dm": bool(e.get("dm")),
                    "channels": e.get("channels") or [],
                    "post_channels": e.get("post_channels") or [],
                }
        self.files.commands.publish(body, check)

    def upkeep_commands(self, state, queue, now):
        """(owner_dm_upkeep) Drop owner items no longer queued; mint one for
        a queued owner command that has none (state.json lost); rewrite a
        lost or mangled owner_items.json. Then the same for reply nonces."""
        e = state.setdefault("owner_items", {})
        queued = {
            str(q["message_id"]): q for q in queue if isinstance(q, dict) and q.get("message_id")
        }
        for mid in [
            m
            for m, v in e.items()
            if m not in queued
            and (not isinstance(v, dict) or now - v.get("at", 0) > OWNER_ITEM_GRACE_SECS)
        ]:
            del e[mid]
        for mid, q in queued.items():
            if (
                mid not in e
                and q.get("owner_command") is True
                and str(q.get("author_id")) in self.auth.owners
            ):
                self.note_command(
                    state,
                    mid,
                    dm=queued_owner_dm(state, q, self.auth.owners),
                    command=OwnerCommand(
                        channel_id=q.get("channel_id"),
                        content=q.get("content") or "",
                        guild_id=q.get("guild_id"),
                    ),
                )
        self.write_commands(state, check=True)
        self.upkeep_replies(state, queued, now)

    def mint_reply(self, state, mid):
        """Queue item mid's reply_nonce (minted when it has none), written to
        item_nonces.json before the item reaches the queue file."""
        n = state.setdefault("reply_nonces", {})
        e = n.get(str(mid))
        if not (isinstance(e, dict) and e.get("nonce")):
            n[str(mid)] = e = {"nonce": secrets.token_hex(16), "at": time.time()}
            keep_last(n, 1000)
        self.write_replies(state)
        return e["nonce"]

    def write_replies(self, state, check=False):
        body = {
            k: v["nonce"]
            for k, v in (state.get("reply_nonces") or {}).items()
            if isinstance(v, dict) and v.get("nonce")
        }
        if not self.files.replies.publish(body, check):
            # The wake hook cannot hand out nonces it cannot read, so no
            # reply can be sent: say so (not "the prompt is out of date").
            day = time.strftime("%Y-%m-%d")
            if self.reply_alert["day"] != day:
                self.reply_alert["day"] = day
                self.notify(
                    "I can't save files in my folder, so I can't "
                    "send replies right now. The disk may be full, "
                    "or the folder's permissions changed."
                )

    def upkeep_replies(self, state, queued, now):
        """Every queued item holds a live reply_nonce (minted again when
        state.json was lost); ones no longer queued are dropped."""
        n = state.setdefault("reply_nonces", {})
        for mid in [
            m
            for m, v in n.items()
            if m not in queued
            and (not isinstance(v, dict) or now - v.get("at", 0) > OWNER_ITEM_GRACE_SECS)
        ]:
            del n[mid]
        for mid, q in queued.items():
            if q.get("kind") == PROACTIVE_KIND:
                continue  # a heartbeat slot: its slot nonce binds it
            if not (n.get(mid) or {}).get("nonce"):
                n[mid] = {"nonce": secrets.token_hex(16), "at": now}
        self.write_replies(state, check=True)
