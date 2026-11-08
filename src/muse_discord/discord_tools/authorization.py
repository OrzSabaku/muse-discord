"""Authorize one owner's live command and restrict it to its original named targets.

A private owner-DM capability is also checked against the source message in that
DM. Neither channel history nor tool output grants authority. Missing settings
keep all DMs guarded; capability checks never spend or rewrite the saved nonce.
"""

import hmac
import json
import re
from typing import Any

from muse_discord.discord_tools.session import ToolSession

DM_TYPES = (1, 3)  # DM, group DM


def _matches(want, got):
    return bool(want) and hmac.compare_digest(want.encode(), got.encode("utf-8", "replace"))


def _for_id(mid):
    if not re.fullmatch(r"[1-9][0-9]{16,19}", mid):
        raise SystemExit(f"Refusing: --for {mid!r} is not a Discord id.")


class CommandAuthorization:
    def __init__(self, session: ToolSession):
        self.session = session
        self.binding: dict[str, Any] = {
            "for": None,
            "nonce": None,
            "entry": None,
            "cmd": None,
            "standalone": False,
        }

    def _live_nonce(self, message_id, path=None):
        try:
            with open(path or self.session.owner_dm_nonces_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        want = data.get(message_id) if isinstance(data, dict) else None
        if isinstance(want, dict):  # owner_items.json: {nonce, channels, ...}
            want = want.get("nonce")
        return want if isinstance(want, str) and want else None

    def _owner_entry(self, message_id):
        try:
            with open(self.session.owner_items_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        e = data.get(message_id) if isinstance(data, dict) else None
        return e if isinstance(e, dict) else {}

    def require_owner_command(self, lookup=False):
        """Refuse unless --for names an open owner-command queue item and
        --nonce is its owner_command_nonce (see the module docstring); for a
        lookup, an open owner DM's owner_dm_nonce does too. Keeps the item's
        record (its channel and the places the owner's own words name) in
        binding["entry"] for guard_target.

        When --standalone is set, authorization is granted locally for server
        channels without requiring an active queue item or nonce."""
        if self.binding.get("standalone"):
            self.binding["entry"] = {}
            return
        mid, got = self.binding["for"], self.binding["nonce"]
        if not mid or not got:
            raise SystemExit(
                "Refusing: this subcommand is for an owner's command only. "
                "Put --for <the item's message_id> --nonce <its "
                "owner_command_nonce> before the subcommand."
            )
        _for_id(mid)
        if _matches(self._live_nonce(mid, self.session.owner_items_file), got):
            self.binding["entry"] = self._owner_entry(mid)
            return
        if lookup and _matches(self._live_nonce(mid), got):
            self.binding["entry"] = {}
            return
        raise SystemExit(
            f"Refusing: {mid} is not an open owner command, "
            "or --nonce is not its owner_command_nonce."
        )

    def guard_target(self, cid):
        """guard_channel, then: the command acts only in its own channel, or
        in one the owner's own words name, as the monitor resolved them to
        ids when it queued the command (owner_items.json: `channels`, from a
        <#id> mention or a #name in the command's server; `post_channels`,
        a channel named bare after "in"/"to"/"into", for `post` only); and
        never in a DM other than its own. Places named only in history or
        in `read` output never count. Ids are compared, nothing else.

        In standalone mode, server channels pass without an owner queue item."""
        ch = self.guard_channel(cid)
        if self.binding.get("standalone"):
            return
        e = self.binding.get("entry") or {}
        if cid == str(e.get("channel_id") or ""):
            return
        if isinstance(ch, dict) and ch.get("type") in DM_TYPES:
            raise SystemExit(f"Refusing: {cid} is a DM, and not this command's own.")
        if cid in (e.get("channels") or []):
            return
        if self.binding.get("cmd") in ("post", "send-message") and cid in (
            e.get("post_channels") or []
        ):
            return
        raise SystemExit(
            f"Refusing: the owner's own message does not name "
            f"{cid} (as <#id>, as #name in this server, or for a "
            f'post, by its name after "in" or "to"), and '
            f"it is not the command's own channel."
        )

    def guard_channel(self, cid):
        """Refuse to act in an owner's DM channel unless this run holds that
        DM (see the module docstring). Server channels pass untouched.
        In standalone mode, all DMs are refused."""
        ch = self.session.api("GET", f"/channels/{cid}")
        if not isinstance(ch, dict) or ch.get("type") not in DM_TYPES:
            return ch
        if self.binding.get("standalone"):
            raise SystemExit(
                f"Refusing: {cid} is a DM. Standalone mode operates in server channels only."
            )
        owners = self.session.owners()
        recips = {str((r or {}).get("id")) for r in ch.get("recipients") or []}
        # A DM with someone else is not an owner's; one with no recipients
        # listed is guarded (failing closed).
        if ch.get("type") == 1 and owners is not None and recips and not recips & owners:
            return ch
        mid, got = self.binding["for"], self.binding["nonce"]
        if not mid or not got:
            raise SystemExit(
                f"Refusing: {cid} is an owner's DM. Only a run handed that DM "
                f"may act there: put --for <its message_id> --nonce <its "
                f"owner_dm_nonce> before the subcommand."
            )
        _for_id(mid)
        if not _matches(self._live_nonce(mid), got):
            raise SystemExit(f"Refusing: --nonce is not the live owner_dm_nonce of {mid}.")
        try:
            self.session.api("GET", f"/channels/{cid}/messages/{mid}")
        except SystemExit:
            raise SystemExit(f"Refusing: {mid} is not a message in {cid}.") from None
        return ch
