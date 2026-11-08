"""Build the conversation and effective-settings context handed to a reply worker."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from muse_discord.conversation import Conversation, _who
from muse_discord.directory.state import DirectoryState
from muse_discord.message_models import Speaker
from muse_discord.watches.requests import WatchRequests


@dataclass
class WorkerContext:
    conversation: Conversation
    directory: DirectoryState
    owners: Callable[[], set[str]]
    get: Callable[[str], Any]
    media: Callable[[dict], tuple[list, list, list]]
    watches: WatchRequests

    def reply_context(self, msg):
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
                rm = self.get(f"/channels/{msg['channel_id']}/messages/{ref_id}")
            except Exception:
                return None
        if not rm:
            return None
        author = _who(rm)
        return {
            "author": author.get("username", "?"),
            "author_id": author.get("id"),
            "content": (rm.get("content") or "")[:500],
            "is_bot": author.get("id") == self.conversation.bot_id,
        }

    def settings_card(self, scope, here, channel_id, speaker=None):
        """The per-message settings card for the worker (item "settings"):
        where it is, who is speaking, who can read the reply, and the style
        and privacy settings that steer it. In an owner's DM, memory is
        always "everything"."""
        speaker = speaker or Speaker()
        author_id = speaker.author_id
        from_bot = speaker.from_bot
        exchange = speaker.exchange
        dm = scope.is_dm
        owner = str(author_id) in self.owners()
        card: dict[str, object] = {"where": "dm" if dm else "server"}
        if not dm:
            info = self.directory.channels.get(channel_id) or {}
            card["server"] = (
                (self.directory.guilds.get(scope.guild_id) or {}).get("name")
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
