"""Resolve command targets only from the owner's own words and known channels."""

import re
from dataclasses import dataclass

from muse_discord.conversation import THREAD_TYPES as _THREAD_TYPES

MIN_BARE_TRIGGER_LENGTH = 3


def owner_own_words(content):
    """The owner's own words in a message: without quoted lines ("> ...")
    and code (``` blocks and `inline`), which may be someone else's
    text pasted in."""
    text = content if isinstance(content, str) else ""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.DOTALL)
    text = re.sub(r"`[^`\n]*`", " ", text)
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith(">"))


def _norm_name(text):
    """Lowercased, "-" and "_" read as spaces, whitespace collapsed."""
    return " ".join(re.sub(r"[-_]", " ", str(text).lower()).split())


BARE_NAME_STOPWORDS = frozenset(
    [
        "the",
        "a",
        "an",
        "to",
        "in",
        "into",
        "on",
        "of",
        "for",
        "and",
        "or",
        "but",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "here",
        "there",
        "me",
        "you",
        "him",
        "her",
        "us",
        "them",
        "my",
        "your",
        "our",
        "their",
        "all",
        "any",
        "some",
        "channel",
        "server",
        "thread",
    ]
)


@dataclass
class CommandTargets:
    channels: dict
    guild_channels: dict

    def server_channels(self, gid):
        """{id: name} of a server's channels the bot knows (no threads, no
        categories)."""
        out = {}
        for c in self.guild_channels.get(str(gid)) or []:
            if isinstance(c, dict) and c.get("type") not in (4, *_THREAD_TYPES):
                out[str(c.get("id"))] = str(c.get("name") or "")
        for cid, info in self.channels.items():
            if (
                str(info.get("guild_id")) == str(gid)
                and not info.get("thread")
                and str(cid) not in out
            ):
                out[str(cid)] = str(info.get("name") or "")
        return out

    def resolve(self, content, guild_id=None):
        """Where an owner's own words point, as channel ids resolved now, at
        queue time: (channels, post_channels). channels: <#id> mentions (the
        only way to name a thread) and #names, resolved among the command's
        own server's channels (an owner DM: in the one server that has a
        channel of that name). post_channels, for `post` only: a channel of
        the command's own server whose whole name (3+ characters, not a
        common word; "-", "_" and spaces alike) follows "in", "to" or
        "into" ("post this in announcements"). Quotes and code are not the
        owner's words (owner_own_words)."""
        text = owner_own_words(content)
        ids = set(re.findall(r"<#([1-9][0-9]{16,19})>", text))
        names = {
            n.lower()
            for n in re.findall(r"(?<![\w&<#])#([^\s#<>@:,;!?()\[\]{}'\"`*~|]{1,100})", text)
        }
        if guild_id:
            pools = [self.server_channels(guild_id)]
        else:
            pools = [
                self.server_channels(g)
                for g in {str(i.get("guild_id")) for i in self.channels.values()}
                | set(self.guild_channels)
            ]
        for name in names:
            hits = {cid for pool in pools for cid, n in pool.items() if n.lower() == name}
            if guild_id or len(hits) == 1:
                ids |= hits
        post = set()
        if guild_id:
            words = _norm_name(text)
            for cid, n in self.server_channels(guild_id).items():
                bare = _norm_name(n)
                if (
                    len(bare) >= MIN_BARE_TRIGGER_LENGTH
                    and bare not in BARE_NAME_STOPWORDS
                    and re.search(
                        r"(?<!\w)(?:in|to|into) (?:the )?" + re.escape(bare) + r"(?!\w)", words
                    )
                ):
                    post.add(cid)
        return sorted(ids), sorted(post - ids)
