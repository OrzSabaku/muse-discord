"""Release notices and owner choices, with durable state supplied by the caller.

on_button expects owner authentication from the interaction dispatcher and only
queues the choice; apply_choice runs on the state-owning main loop.
"""

import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.interactions import closed_card, ephemeral
from muse_discord.owner.messaging import OwnerMessenger
from muse_discord.storage import atomic_write
from muse_discord.storage import read_json as _read_json

UPDATE_ENTRIES_SHOWN = 5
UPDATE_DM_TRIES = 10


def release_version(value):
    """(major, minor, patch) for a release tag "vX.Y.Z", else None
    (pre-releases such as v1.2.0-rc.1 and other tags are not releases)."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    return tuple(int(x) for x in m.groups()) if m else None


def version_installed(root, tag, timeout=5):
    """True when HEAD already contains the release tag (updated by hand
    before the notice went out, or since). False when git cannot tell,
    e.g. the tag was never fetched."""
    try:
        r = subprocess.run(
            ["git", "-C", root, "merge-base", "--is-ancestor", f"refs/tags/{tag}", "HEAD"],
            capture_output=True,
            timeout=timeout,
        )
    except Exception:
        return False
    return r.returncode == 0


def _entry_line(heading):
    """A CHANGELOG release heading "vX.Y.Z (YYYY-MM-DD): Title" as
    "vX.Y.Z: Title"; anything else as is."""
    m = re.fullmatch(r"(v\S+)\s+\([^)]*\):\s*(.+)", heading)
    return f"{m.group(1)}: {m.group(2)}" if m else heading


def notice_text(info):
    version = info["latest"]
    installed = info.get("installed")
    on = (
        f"you're on {installed}"
        if release_version(installed)
        else "you're on an unreleased version"
    )
    lines = [f"**muse-discord {version} is out** ({on})."]
    entries = [_entry_line(str(e))[:150] for e in info.get("entries") or [] if e]
    if entries:
        lines.append("What's new:")
        lines += ["- " + e for e in entries[:UPDATE_ENTRIES_SHOWN]]
        if len(entries) > UPDATE_ENTRIES_SHOWN:
            lines.append(f"- +{len(entries) - UPDATE_ENTRIES_SHOWN} more")
    else:
        lines.append("See the changelog for what's new.")
    if info.get("action_needed"):
        lines.append("Some changes need a step from you; your Muse walks you through them.")
    lines.append(
        f"**Update now**: your Muse updates to {version} in its "
        "main chat and asks before touching anything you "
        f"changed. **Later**: skip {version}; I'll ask again when "
        "a newer version comes out."
    )
    return "\n".join(lines)[:1990]


def buttons(version):
    return [
        {
            "type": 1,
            "components": [
                {"type": 2, "style": 1, "label": "Update now", "custom_id": f"mdupd:now:{version}"},
                {"type": 2, "style": 2, "label": "Later", "custom_id": f"mdupd:later:{version}"},
            ],
        }
    ]


@dataclass
class UpdateNotices:
    check_path: Callable[[], str]
    request_path: Callable[[], str]
    messenger: OwnerMessenger
    enabled: Callable[[], bool]
    backing_off: Callable[[], bool]
    installed: Callable[..., bool]
    announced: dict = field(default_factory=lambda: {"version": None})

    def restore(self, state):
        """Restore the announced version before the interaction worker accepts clicks."""
        self.announced["version"] = (state.get("update_notice") or {}).get("version")

    def announce(self, state):
        """DM the owners about the release in update_check.json, once per
        version: only a version newer than the last one announced is
        offered. Owners whose DM failed are retried each cycle."""
        upd = state.setdefault("update_notice", {})
        self.announced["version"] = upd.get("version")
        if not self.enabled():
            return
        info = _read_json(self.check_path()) or {}
        version = info.get("latest")
        new = release_version(version)
        last = release_version(upd.get("version"))
        if new and (last is None or new > last):
            upd.clear()
            upd.update(version=version, at=int(time.time()), owed=[], tries=0)
            if self.installed(version):
                print(f"Update {version} is already installed; no notice.", flush=True)
            elif not self.messenger.owners:
                print(f"Update {version} available (no owners configured to tell).", flush=True)
            else:
                upd.update(owed=sorted(self.messenger.owners), text=notice_text(info))
                print(f"Update {version} available: DMing the owners.", flush=True)
            self.announced["version"] = version
        owed = upd.get("owed") or []
        if not owed or self.backing_off():
            return
        payload = {
            "content": upd.get("text") or "",
            "allowed_mentions": {"parse": []},
            "components": buttons(upd["version"]),
        }
        upd["owed"] = [uid for uid in owed if not self.messenger.dm(uid, payload)]
        upd["tries"] = upd.get("tries", 0) + 1
        if upd["owed"] and upd["tries"] >= UPDATE_DM_TRIES:
            print(
                f"Update notice undeliverable to {', '.join(upd['owed'])}; giving up.", flush=True
            )
            upd["owed"] = []

    def on_button(self, ix, rest, uid, record):
        action, _, version = rest.partition(":")
        if action not in ("now", "later") or not release_version(version):
            return ephemeral("That control is no longer active.")
        announced = self.announced["version"]
        if version != announced:
            version_key, announced_key = release_version(version), release_version(announced)
            if (
                version_key is not None
                and announced_key is not None
                and version_key < announced_key
            ):
                return ephemeral(
                    f"This message is out of date: {announced} "
                    f"is out now. Use the newest update message."
                )
            return ephemeral("That control is no longer active.")
        # Short timeout: the whole answer must land within Discord's 3s.
        if self.installed(version, timeout=1):
            return ephemeral(f"{version} is already installed; nothing to do.")
        now = int(time.time())
        if action == "now":
            if (_read_json(self.request_path()) or {}).get("version") != version:
                try:
                    atomic_write(
                        self.request_path(), {"version": version, "requested_at": now, "by": uid}
                    )
                except OSError as e:
                    print(f"Could not write update_request.json: {e}", flush=True)
                    return ephemeral("I could not record the request; try again in a moment.")
            line = (
                f"**Update now** chosen. Your Muse is starting the update "
                f"to {version} in its main chat."
            )
        else:
            line = f"**Later** chosen. I won't offer {version} again, only newer versions."
        record(
            {"kind": "update_choice", "version": version, "action": action, "by": uid, "at": now}
        )
        return closed_card(ix, line)

    def apply_choice(self, state, rec):
        upd = state.setdefault("update_notice", {})
        if rec.get("version") == upd.get("version"):
            upd["choice"] = {k: rec.get(k) for k in ("action", "by", "at")}
        print(
            f"Owner {rec.get('by')} chose "
            f"{'Update now' if rec.get('action') == 'now' else 'Later'} "
            f"for update {rec.get('version')}.",
            flush=True,
        )
