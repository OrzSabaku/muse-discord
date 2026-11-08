"""Save plain, secret-free facts from authorized original owner DMs only."""

import hmac
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from muse_discord.message_queue import keep_last, trigger_of
from muse_discord.owner.authorization import OwnerAuthorization, item_nonce

MEMORY_FACTS_PER_ITEM = 5
MEMORY_FACT_MAX = 300
MEMORY_PER_DAY = 20


def memory_line(text):
    """One fact as one plain line: whitespace collapsed, no leading
    markdown (a heading, quote or list mark), at most MEMORY_FACT_MAX."""
    line = " ".join(str(text).split()).lstrip("#>-*+|`= ").strip()
    return line[:MEMORY_FACT_MAX]


@dataclass
class OwnerMemory:
    path: str
    auth: OwnerAuthorization
    enabled: Callable[[], bool]
    secret_in: Callable[[str], str | None]
    missing: dict = field(default_factory=lambda: {"logged": False})

    def check(self, state, item, qid, index):
        """(why the item is refused, or None; the owner DM whose facts to
        save once the reply settles, or None). remember is refused unless
        the item answers an owner's own DM with its live nonce; it is
        skipped (logged) for a DM that is not the owner's own words: a
        reminder or watch notice, a forward, attachments or a reply to
        another message, or an author who is no longer an owner."""
        if not item.get("remember"):
            return None, None
        t = trigger_of(state, qid, index["frags"]) if qid else None
        if not t or not (self.auth.is_owner_dm_item(state, t) or t in index["owners"]):
            return "remember: only in an owner's own DM", None
        want = ((state.get("nl_nonces") or {}).get(t) or {}).get("nonce")
        got, _problem = item_nonce(item)
        if (
            not want
            or not got
            or not hmac.compare_digest(str(want).encode(), got.encode("utf-8", "replace"))
        ):
            return "remember: needs the owner DM's live nonce", None
        meta = (state.get("item_meta") or {}).get(t) or {}
        if (
            meta.get("kind") != "dm"
            or not meta.get("owner_dm")
            or str(meta.get("author_id")) not in self.auth.owners
            or meta.get("forwarded")
            or meta.get("has_inputs")
        ):
            print(
                f"Not saving facts from owner DM {t} to memory: not the "
                f"owner's own words (a notice, forward, attachment or "
                f"reply), or not an owner now.",
                flush=True,
            )
            return None, None
        return None, t

    def _append(self, lines):
        try:
            with open(self.path, "rb") as f:
                end = f.seek(0, os.SEEK_END)
                if end:
                    f.seek(end - 1)
                gap = b"\n" if end and f.read(1) != b"\n" else b""
            with open(self.path, "ab") as f:  # appended: one write
                f.write(gap + "".join(lines).encode("utf-8", "replace"))
        except OSError as e:
            print(f"Could not save facts to memory: {e}", flush=True)
            return False
        return True

    def _fact_lines(self, item, today, n):
        lines = []
        for fact in item["remember"][:MEMORY_FACTS_PER_ITEM]:
            flat = " ".join(str(fact).split())
            if self.secret_in(flat):
                print("Not saving a fact to memory: it looks like a secret.", flush=True)
                continue
            line = memory_line(flat)
            if not line or n + len(lines) >= MEMORY_PER_DAY:
                continue
            lines.append(f"- {today} (told me in a Discord DM): {line}\n")
        return lines

    def save(self, state, item, t):
        """Append item's remember facts (owner DM t, from remember_check)
        to MEMORY_FILE, once per owner DM. Never raises."""
        if not t or not item.get("remember"):
            return
        done = state.setdefault("remembered", {})
        if t in done:
            return  # a resend of an item already saved
        if not self.enabled():
            print("Not saving facts to memory: memory.save_dms is off.", flush=True)
            return
        if not os.path.isfile(self.path):
            if not self.missing["logged"]:
                self.missing["logged"] = True
                print(
                    f"Not saving facts to memory: no {self.path} (set "
                    f"MUSE_MEMORY_FILE to the Muse's memory file).",
                    flush=True,
                )
            return
        today = time.strftime("%Y-%m-%d", time.gmtime())
        used = state.get("remembered_day") or {}
        n = used.get("n", 0) if used.get("day") == today else 0
        lines = self._fact_lines(item, today, n)
        if lines:
            if not self._append(lines):
                return
            print(f"Saved {len(lines)} fact(s) from owner DM {t} to {self.path}.", flush=True)
        done[t] = int(time.time())
        keep_last(done, 300)
        state["remembered_day"] = {"day": today, "n": n + len(lines)}
