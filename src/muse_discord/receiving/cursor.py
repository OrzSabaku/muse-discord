"""Keep REST repair watermarks separate from Gateway delivery deduplication."""

from dataclasses import dataclass

from muse_discord.conversation import _snowflake_ts

GW_SEEN_PER_CHANNEL = 1000
GW_SEEN_MAX_AGE_SECS = 24 * 3600


@dataclass
class ReceiveCursor:
    state: dict

    def seen(self, cid, mid):
        return mid in self.state.get("gw_seen", {}).get(cid, ())

    def note_gateway(self, cid, mid):
        """Record a message the gateway path handled. The first message ever
        seen in a channel seeds its REST watermark instead (history before it
        counts as seen, as with seed_channel_seen)."""
        if cid not in self.state["last_seen"]:
            self.state["last_seen"][cid] = mid
            return
        ids = self.state.setdefault("gw_seen", {}).setdefault(cid, [])
        ids.append(mid)
        del ids[:-GW_SEEN_PER_CHANNEL]

    def advance_rest(self, cid, mid):
        """Move a channel's REST watermark to mid; gateway ids at or below it
        are covered by the watermark now and are dropped."""
        self.state["last_seen"][cid] = mid
        ids = self.state.get("gw_seen", {}).get(cid)
        if ids is not None:
            ids[:] = [i for i in ids if int(i) > int(mid)]
            if not ids:
                del self.state["gw_seen"][cid]

    def prune(self, now):
        """Drop gateway ids no poll will ever re-read (channels nobody polls,
        such as DMs under dm_policy: open)."""
        for cid in list(self.state.get("gw_seen", {})):
            ids = [
                i
                for i in self.state["gw_seen"][cid]
                if now - _snowflake_ts(i) < GW_SEEN_MAX_AGE_SECS
            ]
            if ids:
                self.state["gw_seen"][cid] = ids
            else:
                del self.state["gw_seen"][cid]
