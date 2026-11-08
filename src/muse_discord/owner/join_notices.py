"""Explain newly joined servers to owners after membership checks settle."""

from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import muse_settings as MS
from muse_discord.owner.messaging import OwnerMessenger
from muse_discord.owner.presence import OwnerPresence

DIRECTORY_PREVIEW_LIMIT = 40


def configure_button(gid, prefix):
    """The join notice's button: opens that server's /settings page
    (handled by settings_ui, custom_id mdset:open:server:<id>)."""
    return [
        {
            "type": 1,
            "components": [
                {
                    "type": 2,
                    "style": 1,
                    "label": "Configure",
                    "custom_id": f"{prefix}:open:server:{gid}",
                }
            ],
        }
    ]


@dataclass
class JoinNotices:
    presence: OwnerPresence
    messenger: OwnerMessenger
    backing_off: Callable[[], bool]
    settings_prefix: str

    def summary(self, gid, ginfo, channels):
        name = ginfo.get("name") or gid
        visible = sorted(
            (info.get("name", "?"), info.get("can_send"))
            for info in channels.values()
            if info.get("guild_id") == gid and not info.get("thread")
        )
        shown = ", ".join(
            "#" + n + (" (read-only)" if s is False else "")
            for n, s in visible[:DIRECTORY_PREVIEW_LIMIT]
        )
        if len(visible) > DIRECTORY_PREVIEW_LIMIT:
            shown += f", and {len(visible) - DIRECTORY_PREVIEW_LIMIT} more"
        st = self.presence.settings_source()
        res = st.resolve(MS.Scope.server(gid, name))
        seen = f"Channels I can see ({len(visible)}): {shown or 'none'}\n"
        if self.presence.absent(gid) and self.presence.membership.get(gid) is None:
            return (
                f"I was added to **{name}**. I couldn't check yet whether "
                f"any of you is in that server, so I'm not answering "
                f"anyone there for now.\n{seen}"
                f"To let me talk there, press **Configure** and pick When "
                f"it answers."
            )
        if self.presence.absent(gid):
            return (
                f"I was added to **{name}**, a server none of you is in, "
                f"so I'm not answering anyone there.\n{seen}"
                f"To let me talk there, press **Configure** and pick When "
                f"it answers. To remove me, a server admin there can kick "
                f"me."
            )
        replies = {
            "freely": "I may join conversations without being mentioned there",
            "off": "I stay quiet there",
            "when-addressed": "I answer @mentions and replies to my messages there",
            "conversational": "I answer @mentions and replies to my "
            "messages there, and the follow-ups of "
            "someone I just answered",
        }
        on = ["image and video generation"] if res.get("media.enabled") else []
        return (
            f"I just joined **{name}**.\n"
            f"{replies.get(res['who.replies'], replies['when-addressed'])}"
            f". Extras on there: {', '.join(on) or 'none'}.\n{seen}"
            f"To change what I do there, press **Configure** or use "
            f"/settings."
        )

    def flush(self, state, guilds, channels):
        """Send owed join summaries; anything undelivered is retried later."""
        notices = state.get("join_notices") or []
        if not notices or self.backing_off():
            return
        left = []
        for gid in notices:
            if gid not in state.get("known_guilds", {}):
                continue  # removed again before the notice went out
            if self.presence.pending(state, gid):
                left.append(gid)  # say what the owner check found
                continue
            if not self.messenger.notify(
                self.summary(gid, guilds.get(gid) or {}, channels),
                components=configure_button(gid, self.settings_prefix),
            ):
                left.append(gid)
        state["join_notices"] = left
