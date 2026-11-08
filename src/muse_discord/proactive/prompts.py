"""Build a proactive slot and the bounded context its isolated worker receives.

Describe eligible places using history metadata and effective settings. The
runtime persists the result before publishing its nonce and queue item.
"""

import secrets
from dataclasses import dataclass
from datetime import datetime

from muse_discord import muse_settings as MS
from muse_discord.message_models import Speaker
from muse_discord.proactive.model import DAY, FIELD, KIND, MAX_PER_SLOT, SLOT_SECS, ProactiveContext
from muse_discord.proactive.store import ProactiveStore
from muse_discord.proactive.targets import TargetPolicy
from muse_discord.proactive.timing import iso, snowflake_ts, zone


@dataclass
class SlotPrompts:
    context: ProactiveContext
    store: ProactiveStore
    policy: TargetPolicy

    def describe(self, state, t, now):
        """What the worker gets about one target: who or where, when they
        last talked and how that ended, the last unprompted message, and
        the settings card of the place. No message text."""
        context = self.context
        cid, uid = t["channel_id"], t.get("user_id")
        hist = self.policy.history(state, cid)
        out = {"channel_id": cid, "type": t["type"]}
        mine = self.policy.sent_to("channel_id", cid, 0)
        out["last_unprompted"] = iso(max([e["at"] for e in mine] or [0]))
        if t["type"] == "channel":
            info = context.channels().get(cid) or {}
            gid = info.get("guild_id")
            scope = context.scope_for(gid, cid)
            card = context.settings_card(
                scope,
                self.policy.st().resolve(scope),
                cid,
                speaker=Speaker(author_id=None, from_bot=False),
            )
            card.pop("speaker", None)
            humans = [h for h in hist if not self.policy.is_bot_msg(h)]
            out.update(
                where=context.channel_display(info),
                last_message_at=iso(snowflake_ts(hist[-1]["id"])) if hist else None,
                recent_messages=len([h for h in humans if now - snowflake_ts(h.get("id")) < DAY]),
                recent_people=len(
                    {h.get("author_id") for h in humans if now - snowflake_ts(h.get("id")) < DAY}
                ),
            )
        else:
            p = self.store.data()["people"].setdefault(uid, {})
            name = (
                context.user_names().get(uid)
                or p.get("name")
                or context.dm_channels().get(cid)
                or uid
            )
            if name != uid and p.get("name") != name:
                p["name"] = name
                self.store.dirty = True
            scope = MS.Scope.dm()
            card = context.settings_card(
                scope,
                self.policy.st().resolve(scope),
                cid,
                speaker=Speaker(author_id=uid, from_bot=False),
            )
            card.pop("places", None)
            last_them = max(
                [p.get("last", 0)]
                + [snowflake_ts(h.get("id")) for h in hist if str(h.get("author_id")) == uid]
            )
            last_you = max(
                [0] + [snowflake_ts(h.get("id")) for h in hist if self.policy.is_bot_msg(h)]
            )
            outcome = None
            if p.get("last_mid"):
                e = context.ledger()["entries"].get(p["last_mid"]) or {}
                outcome = e.get("code")
            tz = self.policy.tz_of(uid)
            out.update(
                user_id=uid,
                name=name,
                owner=t["type"] == "owner",
                local_time=datetime.fromtimestamp(now, zone(tz)).strftime("%A %H:%M"),
                timezone=tz,
                where=f"DM with {name}",
                last_from_them=iso(last_them),
                last_from_you=iso(last_you),
                ended_with=None
                if not (last_them or last_you)
                else ("you" if last_you > last_them else "them"),
                their_last_message=outcome,
            )
        out["settings"] = card
        return out

    def build(self, state, targets, now):
        sid = self.context.started_item_id(now)  # the fires' id scheme
        slot = {
            "id": sid,
            "kind": KIND,
            "nonce": secrets.token_hex(16),
            "at": now,
            "expires": now + SLOT_SECS,
            "max_posts": MAX_PER_SLOT,
            "spent": False,
            "posts": {},
            "targets": {
                t["channel_id"]: {"type": t["type"], "user_id": t.get("user_id")} for t in targets
            },
        }
        tzname = self.policy.every("proactive.timezone")
        local = datetime.fromtimestamp(now, zone(tzname))
        item = {
            "message_id": sid,
            "kind": KIND,
            "channel_id": None,
            "channel_name": "(no one asked: your call)",
            "guild_id": None,
            "author": None,
            "author_id": None,
            "content": "",
            "timestamp": iso(now),
            "queued_at": now,
            "media": [],
            "documents": [],
            "unviewable": [],
            "history": [],
            "may_generate": False,
            "owner_command": False,
            "reply_to": None,
            "burst": False,
            "burst_fragments": [],
            "from_bot": False,
            "bot_turns": 0,
            "participants": [],
            "follow_up": False,
            "settings": None,
            FIELD: {
                "expires_at": iso(now + SLOT_SECS),
                "max_messages": MAX_PER_SLOT,
                "local_time": local.strftime("%A %H:%M"),
                "timezone": tzname,
                "targets": [self.describe(state, t, now) for t in targets],
            },
        }
        slot["pending_item"] = item
        return slot, item
