"""Collect administrator changes and send bounded per-server owner digests."""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

from muse_discord import muse_settings as MS

ADMIN_DIGEST_SECS = 60


def _show_value(v):
    if v is True:
        return "on"
    if v is False:
        return "off"
    if v is None:
        return "none"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or "(empty)"
    return str(v)


@dataclass
class AdminDigests:
    directory_name: Callable[[str], str | None]
    user_names: dict
    guilds: dict
    notify: Callable[[str], bool]
    backing_off: Callable[[], bool]
    changes: dict = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)

    def note(self, guild_id, user_id, key, old, new):
        """Queue one line for the owners' digest of server-admin changes
        in that server (sent by the main loop, one DM per server per
        ADMIN_DIGEST_SECS)."""
        gid = str(guild_id)
        with self.lock:
            entry = self.changes.setdefault(gid, {"first": time.time(), "changes": []})
            entry["changes"].append(
                {"user": str(user_id), "key": key, "old": old, "new": new, "at": time.time()}
            )

    def name(self, some_id):
        """A server's, channel's or person's name for the digest's words."""
        return self.directory_name(some_id) or self.user_names.get(str(some_id))

    def words(self, c):
        """ "In #general, When it answers: Chatty → Off" for a change whose
        key is an overlay key (what /settings sends), else None."""
        try:
            path, key = MS.parse_key(c["key"])
            place = MS.place_of_path(path)
            if place is None or MS.CORE.get(key) is None:
                return None
            return MS.describe_change(
                place,
                key,
                names=self.name,
                path=path,
                values=MS.ValueChange(old=c["old"], new=c["new"]),
            )
        except Exception:
            return None

    def text(self, gid, changes):
        name = self.directory_name(gid) or (self.guilds.get(gid) or {}).get("name") or gid
        head = f"**A server admin changed my settings in {name}:**\n"
        tail = (
            "\nAdmins can only make me quieter. "
            "To undo a change, use /settings there and press Undo."
        )
        lines = []
        for c in changes:
            when = datetime.fromtimestamp(c["at"], UTC).strftime("%H:%M UTC")
            words = self.words(c)
            if words:
                lines.append(f"- <@{c['user']}>: {words}, {when}")
                continue
            lines.append(
                f"- <@{c['user']}> set `{c['key']}` to "
                f"{_show_value(c['new'])} (was "
                f"{_show_value(c['old'])}), {when}"
            )
        kept = []
        room = 1900 - len(head) - len(tail) - len("- and 9999 more")
        for line in lines:
            if sum(len(x) + 1 for x in kept) + len(line) > room:
                kept.append(f"- and {len(lines) - len(kept)} more")
                break
            kept.append(line)
        return head + "\n".join(kept) + tail

    def flush(self, force=False):
        """Main loop: DM the owners one digest per server whose first queued
        admin change is ADMIN_DIGEST_SECS old. Undelivered ones are kept and
        retried. Returns how many went out."""
        now = time.time()
        with self.lock:
            due = {
                gid: e
                for gid, e in self.changes.items()
                if force or now - e["first"] >= ADMIN_DIGEST_SECS
            }
        if not due or self.backing_off():
            return 0
        sent = 0
        for gid, entry in due.items():
            if self.notify(self.text(gid, entry["changes"])):
                sent += 1
                with self.lock:
                    cur = self.changes.get(gid)
                    if cur is entry:
                        del self.changes[gid]
            else:
                print(f"Admin digest for server {gid} undelivered; retrying.", flush=True)
        return sent
