"""Fail-closed owner-DM authorization, separate from capability issuance.

Directory snapshots and REST access are explicit inputs. A gate never consumes a
nonce: only successful settlement can spend it, after acquiring the queue lock.
Any reply, silence or reaction that settles an owner DM, or posts in its channel,
needs that item's owner_dm_nonce before other effects are considered. A refused
item stays open for an owner-only wake to answer it.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from muse_discord.message_queue import owner_dm_metadata, trigger_of

OWNER_DM_NONCE_FIELD = "owner_dm_nonce"


def item_nonce(item):
    """The owner-DM capability explicitly attached to this outbox item."""
    nonce = item.get(OWNER_DM_NONCE_FIELD)
    if nonce is not None and not isinstance(nonce, str):
        return None, f"{OWNER_DM_NONCE_FIELD} is not a string"
    return nonce, None


@dataclass
class OwnerAuthorization:
    owners_source: Callable[[], set[str]]
    channels: dict
    guild_channels: dict
    dm_users: dict
    get: Callable[[str], Any]
    lookup: dict = field(default_factory=dict)

    @property
    def owners(self) -> set[str]:
        return self.owners_source()

    def metadata(self, meta):
        return owner_dm_metadata(meta, self.owners)

    def is_owner_dm_item(self, state, mid):
        """True when queue item mid (a trigger's message id) is an owner's
        DM: the monitor minted it a nonce, or its record says so."""
        if not mid:
            return False
        mid = str(mid)
        if mid in (state.get("nl_nonces") or {}):
            return True
        return self.metadata((state.get("item_meta") or {}).get(mid))

    def dm_channels(self, state):
        """The owners' DM channels: resolved ones, and those of owner-DM
        items the monitor recorded."""
        out = {c for c, u in self.dm_users.items() if u in self.owners}
        for meta in (state.get("item_meta") or {}).values():
            if self.metadata(meta) and meta.get("channel_id"):
                out.add(str(meta["channel_id"]))
        for e in (state.get("nl_nonces") or {}).values():
            if isinstance(e, dict) and e.get("channel_id"):
                out.add(str(e["channel_id"]))
        return out

    def channel_of(self, state, qid):
        meta = (state.get("item_meta") or {}).get(qid) or {}
        e = (state.get("nl_nonces") or {}).get(qid) or {}
        cid = meta.get("channel_id") or (e.get("channel_id") if isinstance(e, dict) else None)
        return str(cid) if cid else None

    def channel_kind(self, cid):
        """What the API says channel cid is: "owner_dm", "dm" or "guild", or
        None when it cannot tell. A DM with no recipients listed counts as
        an owner's (fail closed)."""
        if cid not in self.lookup:
            try:
                ch = self.get(f"/channels/{cid}")
            except Exception as e:
                print(f"Could not look up channel {cid}: {e}", flush=True)
                return None
            if not isinstance(ch, dict):
                return None
            self.lookup[cid] = (
                ch.get("type") in (1, 3),
                frozenset(str((r or {}).get("id")) for r in ch.get("recipients") or []),
            )
        dm, recips = self.lookup[cid]
        if not dm:
            return "guild"
        return "owner_dm" if not recips or recips & self.owners else "dm"

    def _unknown_channel_problem(self, state, triggers, cid, index):
        """An outbox item that touches no owner DM, in a channel that is not
        a known owner DM channel: fine in a guild channel the bot reads, in
        the channel of the (non-owner) item it answers, or in a known
        non-owner DM. Anything else is looked up (the monitor's records of
        the owners' DM channels are best effort), failing closed."""
        if cid in self.channels or any(
            str(c.get("id")) == cid for cs in self.guild_channels.values() for c in cs
        ):
            return None
        if len(triggers) == 1:
            t = next(iter(triggers))
            meta = (state.get("item_meta") or {}).get(t) or {}
            if cid in (str(meta.get("channel_id")), index["channels"].get(t)):
                return None
        if cid in self.dm_users:
            return None  # a listed person's DM (owners' were caught above)
        kind = self.channel_kind(cid)
        if kind in ("guild", "dm"):
            return None
        if kind == "owner_dm":
            return "owner DM channel: the item names no owner DM that holds a live nonce"
        return f"channel {cid}: unknown, and could not be looked up"

    def _nonce_problem(self, state, item, qid):
        got, problem = item_nonce(item)
        if problem:
            return f"owner DM {qid}: bad nonce ({problem})"
        want = ((state.get("nl_nonces") or {}).get(qid) or {}).get("nonce")
        if not want:
            return f"owner DM {qid}: bad nonce (none live: answered already, or not handed out yet)"
        if not got:
            return (
                f"owner DM {qid}: bad nonce (missing: copy the item's "
                f"{OWNER_DM_NONCE_FIELD} from the wake payload)"
            )
        if not hmac.compare_digest(str(want).encode(), got.encode("utf-8", "replace")):
            return f"owner DM {qid}: bad nonce (wrong)"
        return None

    def _channel_problem(self, state, triggers, cid, index):
        if cid not in self.dm_channels(state) and cid not in index["owners"].values():
            return self._unknown_channel_problem(state, triggers, cid, index)
        # An item may always answer in its own channel: one queued in
        # this DM before its author became an owner. (Nobody else's
        # item lives in an owner's 1:1 DM.)
        meta = (
            (state.get("item_meta") or {}).get(next(iter(triggers))) if len(triggers) == 1 else None
        )
        if meta and str(meta.get("channel_id")) == cid:
            return None
        return "owner DM channel: the item names no owner DM that holds a live nonce"

    def gate(self, state, item, index=None):
        """None when this (well-formed) outbox item may go on; else why it is
        refused. It must carry the nonce when it touches an owner's DM: its
        item, or its reply_to (or that fragment's trigger), is an owner-DM
        item, or its channel_id is an owner's DM channel. Then item and
        reply_to must all name that one owner DM (item exactly; reply_to it
        or one of its fragments), channel_id must be its channel, and the
        nonce must match (constant time). Never consumes the nonce: settling
        the item does (process_outbox), so retries and resends keep it.
        index: queue_index(), read once per pass."""
        index = index or {"frags": {}, "owners": {}, "channels": {}}
        named = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
        triggers = {trigger_of(state, m, index["frags"]) for m in named}
        owner = {t for t in triggers if self.is_owner_dm_item(state, t) or t in index["owners"]}
        cid = str(item.get("channel_id") or "")
        if not owner:
            return self._channel_problem(state, triggers, cid, index)
        if len(owner) != 1 or len(triggers) != 1:
            return (
                "owner DM: the item must answer exactly one owner DM, "
                "named by item and reply_to, in its own channel"
            )
        qid = owner.pop()
        if item.get("item") and str(item["item"]) != qid:
            return "owner DM: item must be the DM's own message_id"
        if cid != (self.channel_of(state, qid) or index["owners"].get(qid)):
            return f"owner DM {qid}: wrong channel"
        return self._nonce_problem(state, item, qid)
