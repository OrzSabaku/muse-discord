"""Authorize, send and retry only the targets bound to a durable proactive slot.

Accepted text and Discord nonces survive retries. Uncertain sends continue to
count against caps, and slot replies never fall through to ordinary delivery.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus

from muse_discord.message_models import DISCORD_MAX_CHARS, DeliveryOptions, SecretBlocked
from muse_discord.proactive.model import (
    FORBIDDEN,
    MAX_PER_SLOT,
    NONCE_FIELD,
    SEND_RETRY_SECS,
    ProactiveContext,
    Verdict,
)
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy


@dataclass
class SlotDelivery:
    context: ProactiveContext
    store: ProactiveStore
    policy: TargetPolicy
    clock: Callable[[], float]

    def names_slot(self, item):
        """The slot id an outbox item names (item or reply_to; a live or
        spent slot), else None: such an item is bound here, by the slot's
        nonce, never by a reply_nonce (monitor.bound_elsewhere)."""
        if not isinstance(item, dict):
            return None
        d = self.store.data()
        s = d.get("slot") if self.store.slot_ok(d.get("slot")) else {}
        known = set(d["spent"]) | ({s["id"]} if s else set())
        names = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
        return next((n for n in names if n in known), None)

    def outbox_item(self, state, item):
        """None when the outbox item names no slot (it goes the usual way);
        else a Verdict. Posts only to the slot's targets, with its nonce,
        while it is fresh and unspent, within the caps. A damaged record
        never raises: an item naming a slot is then set aside."""
        d = self.store.data()
        s = d.get("slot") if self.store.slot_ok(d.get("slot")) else {}
        sid = self.names_slot(item)
        if sid is None:
            return None
        try:
            return self._decide(state, item, d, s, sid)
        except Exception as e:
            print(f"Proactive: slot {sid}: {type(e).__name__}: {e}", flush=True)
            return Verdict(f"slot {sid}: could not be checked", spent=sid)

    def _slot_binding_problem(self, item, s, sid):
        if str(item.get("item") or "") != sid:
            return Verdict(f"slot {sid}: item must be the slot's message_id")
        bad = [k for k in FORBIDDEN if k in item]
        if bad:
            return Verdict(f"slot {sid}: carries no {', '.join(bad)}")
        if not s or s["id"] != sid:
            return Verdict(f"slot {sid}: already used or expired")
        got = item.get(NONCE_FIELD)
        if not isinstance(got, str) or not hmac.compare_digest(
            s["nonce"].encode(), got.encode("utf-8", "replace")
        ):
            # A forgery from another wake: the slot is untouched.
            return Verdict(f"slot {sid}: bad nonce")
        return None

    def _slot_status_problem(self, s, now):
        sid = s["id"]
        if s.get("spent"):
            return Verdict(f"slot {sid}: already used", spent=sid)
        if self.store.blocked():
            s["spent"] = True
            return Verdict(f"slot {sid}: proactive.json is set aside", spent=sid)
        if now > s.get("expires", 0):
            return Verdict(f"slot {sid}: expired", spent=sid)
        return None

    def _decide(self, state, item, d, s, sid):
        now = self.clock()
        problem = self._slot_binding_problem(item, s, sid)
        if problem is not None:
            return problem
        cid = str(item.get("channel_id") or "")
        post = s["posts"].get(cid)
        if post and post.get("status") == "sending":
            return self._send(state, s, cid, item, now)  # a retry
        problem = self._slot_status_problem(s, now)
        if problem is not None:
            return problem
        if item.get("silent"):
            s["spent"] = True
            self.store.save()
            print(f"Slot {sid}: the worker chose not to reach out.", flush=True)
            return Verdict(spent=sid)
        return self._accept_post(state, item, s, now)

    def _accept_post(self, state, item, s, now):
        sid = s["id"]
        cid = str(item.get("channel_id") or "")
        d = self.store.data()
        t = s["targets"].get(cid)
        if t is None:
            s["spent"] = True
            self.store.save()
            return Verdict(f"slot {sid}: {cid} is not one of its targets", spent=sid)
        t = dict(t, channel_id=cid)
        why = self.policy.target_problem(state, t, now)
        if why is None and len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            why = "the slot's messages are used"
            # Clamped to Discord's limit after the rewrites, as replies are.
        parts = [x[:DISCORD_MAX_CHARS] for x in self.context.normalize_parts(item)]
        if why is None and not parts:
            why = "nothing to send"
        if (
            why is None
            and t["type"] != "owner"
            and self.context.never_share_hit(state, {"channel_id": cid}, None, parts)
        ):
            why = "it contains a never_share phrase (dropped)"
        if why is not None:
            s["spent"] = True
            self.store.save()
            return Verdict(f"slot {sid}: not sent to {cid}: {why}", spent=sid)
        s["posts"][cid] = {"status": "sending", "first": now, "n": 0, "next": 0, "parts": parts}
        if len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            s["spent"] = True
        entry = {
            "at": now,
            "slot": sid,
            "channel_id": cid,
            "user_id": t.get("user_id"),
            "type": t["type"],
        }
        d["sent"].append(entry)
        self.store.save()
        return self._send(state, s, cid, item, now)

    def _send(self, state, s, cid, item, now):
        """Send (or retry) the accepted post for cid: the parts fixed at
        acceptance (the outbox copy is worker-writable), pinging at most
        that one person."""
        sid, post = s["id"], s["posts"][cid]
        if post.get("next", 0) > now:
            return Verdict(retry=True, spent=sid)
        t = s["targets"][cid]
        mentions = {"parse": []}
        if t.get("user_id"):
            mentions["users"] = [t["user_id"]]
        parts = post["parts"]
        try:
            # The monitor's one send path (plain messages: no reply_to),
            # each part's Discord nonce seeded with the slot id.
            msgs = self.context.send_fragmented(
                cid, parts, None, (), delivery=DeliveryOptions(mentions=mentions, nonce=sid)
            )
        except Exception as e:
            return self._send_failure(s, cid, now, e)
        post["status"] = "sent"
        post.pop("parts", None)
        for x in self.store.data()["sent"]:
            if x.get("slot") == sid and x.get("channel_id") == cid:
                x["msgs"] = msgs[-4:]  # for reactions (an answer too)
        self.store.save()
        print(
            f"Slot {sid}: reached out in {cid} ({t['type']}, "
            f"{len(parts)} part(s)): {parts[0][:60]}",
            flush=True,
        )
        return Verdict(spent=sid)

    def _send_failure(self, s, cid, now, e):
        sid, post = s["id"], s["posts"][cid]
        code = getattr(e, "code", None)
        post["n"] = post.get("n", 0) + 1
        # A secret in it (the monitor's send guard): never retried,
        # the slot is used up, nothing counts toward the caps.
        blocked = isinstance(e, SecretBlocked)
        # A part may have landed: any part after the first (on this
        # or an earlier attempt), or the first one when the error
        # does not say it was refused (a timeout can land).
        part = getattr(e, "failed_part", None)
        if part != 0 or getattr(e, "sent_ids", None):
            post["landed"] = True
        refused = (
            isinstance(code, int)
            and HTTPStatus.BAD_REQUEST <= code < HTTPStatus.INTERNAL_SERVER_ERROR
        )
        maybe = not blocked and (post.get("landed") or not refused)
        if code == HTTPStatus.FORBIDDEN or blocked or now - post["first"] >= SEND_RETRY_SECS:
            post["status"] = "failed"
            if maybe or code == HTTPStatus.FORBIDDEN:
                # It counts, as a reply that may have landed does; a
                # 403 (blocked, or no longer allowed there) too, so
                # nobody is tried again before they write.
                for x in self.store.data()["sent"]:
                    if x.get("slot") == sid and x.get("channel_id") == cid:
                        x["msgs"] = list(getattr(e, "sent_ids", None) or [])[-4:]
            else:
                self.store.data()["sent"] = [
                    x
                    for x in self.store.data()["sent"]
                    if not (x.get("slot") == sid and x.get("channel_id") == cid)
                ]
            self.store.save()
            print(
                f"Slot {sid}: could not post to {cid} ({e}); "
                f"dropped{' (part of it may have landed)' if maybe else ''}.",
                flush=True,
            )
            return Verdict(spent=sid)
        post["next"] = now + min(30, 2 ** post["n"])
        self.store.save()
        print(
            f"Slot {sid}: post to {cid} failed (attempt {post['n']}; retrying, same nonce): {e}",
            flush=True,
        )
        return Verdict(retry=True, spent=sid)
