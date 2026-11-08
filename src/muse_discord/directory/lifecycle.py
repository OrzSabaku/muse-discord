"""Guild joins/leaves, owner admission checks and directory-label diagnostics.

The lifecycle returns refresh requests as values. It never changes monitor
scheduler flags or imports its composition root.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.directory.discovery import DiscoveryState
from muse_discord.muse_settings import Settings
from muse_discord.owner.join_notices import JoinNotices
from muse_discord.owner.presence import OwnerPresence
from muse_discord.response_policy import note_guild_joined


@dataclass
class GuildLifecycle:
    directory: DiscoveryState
    channels: dict
    presence: OwnerPresence
    notices: JoinNotices
    settings_source: Callable[[], Settings]
    publish: Callable[[], None]
    allowed_guilds: set[str]
    stale_labels: set = field(default_factory=set)

    def sync(self, state, guilds, channels):
        """Record each guild's join watermark and greet newly joined guilds
        (owner DM). The first run seeds the known set silently, but checks
        every server for an owner (see check_owner_presence)."""
        if not guilds:
            return  # discovery failed; a partial view must not look like joins
        first_run = "known_guilds" not in state
        known = state.setdefault("known_guilds", {})
        notices = state.setdefault("join_notices", [])
        # Approximate member counts, for bin/settings.py's risk checks.
        state["guild_members"] = {
            gid: self.directory.member_counts[gid]
            for gid in guilds
            if isinstance(self.directory.member_counts.get(gid), int)
        }
        for gid, ginfo in guilds.items():
            note_guild_joined(state, gid, ginfo.get("joined_at"))
            is_new = gid not in known
            known[gid] = ginfo.get("name") or gid
            if is_new and not first_run:
                print(
                    f"Joined server {known[gid]} ({gid}): its settings "
                    f"come from all_servers until it has its own.",
                    flush=True,
                )
                if gid not in notices:
                    notices.append(gid)
        self.presence.check(
            state,
            guilds,
            list(guilds) if first_run else [g for g in notices if g in guilds],
            every=first_run,
        )
        self.notices.flush(state, guilds, channels)
        try:
            self.warn_labels()
        except Exception as e:
            print(f"Label check failed: {e}", flush=True)

    def warn_labels(self):
        """A `name:` label in config.yaml is only for people; say so once
        when Discord calls that server or channel something else now."""
        for sid, label in self.settings_source().labels.items():
            now = (self.directory.guilds.get(sid) or {}).get("name") or (
                self.channels.get(sid) or {}
            ).get("name")
            if (
                not now
                or not label
                or str(label).lstrip("#") == now
                or (sid, label) in self.stale_labels
            ):
                continue
            self.stale_labels.add((sid, label))
            print(
                f'Note: config.yaml labels {sid} "{label}", but Discord '
                f'calls it "{now}" now (the label is only for you; '
                f"the id is what counts).",
                flush=True,
            )

    def on_event(self, state, ev):
        """Gateway GUILD_CREATE / GUILD_DELETE. A create for an unknown guild
        is a join: record its watermark and request a discovery pass, which
        then sends the owner summary via sync_guilds."""
        refresh = False
        gid = ev.get("id")
        if not gid or (self.allowed_guilds and gid not in self.allowed_guilds):
            return False
        if ev.get("op") == "create":
            note_guild_joined(state, gid, ev.get("joined_at"))
            if isinstance(ev.get("member_count"), int):
                self.directory.member_counts[gid] = ev["member_count"]
                self.publish()
            known = state.get("known_guilds")
            if known is None or gid not in known:
                # A join, or a first run whose discovery has not finished (a
                # lost state.json): ownerless until the owner check
                # (sync_guilds).
                self.presence.joined(state, gid)
                if known is not None:
                    refresh = True
        else:
            name = state.get("known_guilds", {}).pop(gid, None)
            state.get("owner_in_guild", {}).pop(gid, None)
            self.presence.membership.pop(gid, None)
            state.get("guild_joined", {}).pop(gid, None)
            state.get("guild_first_seen", {}).pop(gid, None)
            print(f"Removed from server {name or gid}.", flush=True)
        return refresh
