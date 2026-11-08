"""Bind a notification reply and keep its target stable across retries."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord import muse_watches as W
from muse_discord.message_models import DISCORD_MAX_CHARS


def _bind_problem(item, fid, f):
    if item.get("item") and str(item["item"]) != fid:
        return f"reminder {fid}: item must be the reminder's message_id"
    if "schedule" in item or "settings" in item:
        return f"reminder {fid}: its reply carries no schedule or settings"
    if str(item.get("channel_id")) != f["channel_id"]:
        return f"reminder {fid}: channel_id must be the reminder's channel"
    return None


@dataclass
class WatchDelivery:
    store: W.Store
    get: Callable[[str], Any]
    normalize_parts: Callable[[dict], list[str]]

    def bind(self, item):
        """An outbox item that answers a fire: None when it answers none;
        else the fire, with the item bound to it (its item set, its
        reply_to, reactions, attachments and standalone dropped: the
        monitor picks where it threads and whom it pings); or why it is
        refused (another channel, an action on it, already answered)."""
        data = self.store.read()
        if not data["fires"] and not data["fired"]:
            return None
        fid = next(
            (
                str(item[f])
                for f in ("item", "reply_to")
                if item.get(f) and (str(item[f]) in data["fires"] or str(item[f]) in data["fired"])
            ),
            None,
        )
        if fid is None:
            return None
        f = data["fires"].get(fid)
        if f is None:
            # Settled, or dropped unanswered: nothing is ever sent for it
            # (unbound), and its queue item, if any, is settled.
            return {"gone": fid}
        problem = _bind_problem(item, fid, f)
        if problem:
            return problem
        item["item"] = fid
        for k in ("reply_to", "reactions", "attachments", "standalone", "remember"):
            item.pop(k, None)
        if not item.get("silent") and not self.normalize_parts(item):
            return f"reminder {fid}: the reply needs text"
        return f

    def _original_target(self, cid, fire):
        for oid in reversed(fire.get("originals") or []):
            try:
                m = self.get(f"/channels/{cid}/messages/{oid}")
            except Exception as e:
                if getattr(e, "code", None) in (403, 404):
                    continue  # deleted (or unreadable): not that one
                raise  # transient: the send is retried
            if isinstance(m, dict) and str(m.get("id")) == str(oid):
                return str(oid)
        return None

    def target(self, state, fire):
        """The message a fire's reply threads under: the newest original
        request still there, else the requester's newest message in the
        place, else None (a plain message). Worked out once and kept with
        the fire, so a retried send has the same nonce."""
        if "target" in fire:
            return fire["target"]
        cid, uid = fire["channel_id"], fire["requester"]
        target = self._original_target(cid, fire)
        if target is None:
            mine = [
                h
                for h in (state.get("history") or {}).get(cid) or []
                if str(h.get("author_id")) == uid
            ]
            if not mine:
                try:
                    got = self.get(f"/channels/{cid}/messages?limit=50") or []
                except Exception:
                    got = []
                mine = [
                    {"id": m["id"]}
                    for m in got
                    if isinstance(m, dict) and str((m.get("author") or {}).get("id")) == uid
                ]
            if mine:
                target = str(max(mine, key=lambda h: int(h["id"]))["id"])
        with self.store.edit() as d:
            if fire["id"] in d["fires"]:
                d["fires"][fire["id"]]["target"] = target
        fire["target"] = target
        return target

    def send(self, state, fire, parts):
        """(reply_to, parts, allowed_mentions) for a reply to a fire: under
        the requester's message (target), else a plain message
        that @mentions them; either way it can ping them and nobody else."""
        uid = fire["requester"]
        target = self.target(state, fire)
        if not target and parts:
            parts = [f"<@{uid}> {parts[0]}"[:DISCORD_MAX_CHARS], *parts[1:]]
        return target, parts, {"parse": [], "users": [uid], "replied_user": True}
