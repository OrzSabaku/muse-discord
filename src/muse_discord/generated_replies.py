"""Bind generated reply attachments to a live item/channel approval.

File containment, compression and disk expiry belong to generated_media. This
service chooses attachments and revalidates worker-writable retry metadata;
it never sends a reply or grants/consumes generation permission itself.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from muse_discord import generated_media

GENERATED_SEND_HISTORY_LIMIT = 50
GENERATED_PATH_PAIR_LENGTH = 2


@dataclass
class GeneratedReplies:
    directory: str
    now: Callable[[], float] = time.time

    def sweep_files(self):
        generated_media.sweep_generated(self.directory)

    def sweep(self, state):
        self.sweep_files()
        self.sweep_state(state)

    def sweep_state(self, state):
        """Drop generation approvals older than generated_media.TTL_SECS (they are
        single-use and short-lived by design) and bound the sent-file record.
        Approvals from before channel binding (a bare timestamp) go too."""
        approved = state.get("gen_approved", {})
        now = self.now()
        for mid in [
            m
            for m, a in approved.items()
            if not isinstance(a, dict) or now - a.get("ts", 0) > generated_media.TTL_SECS
        ]:
            approved.pop(mid, None)
        if len(state.get("gen_sent", [])) > GENERATED_SEND_HISTORY_LIMIT:
            state["gen_sent"] = state["gen_sent"][-GENERATED_SEND_HISTORY_LIMIT:]

    def item_id(self, state, item):
        """The queue item an outbox item answers, as its trigger message id:
        reply_to, resolved through reply_aliases (the worker replies under
        the LAST burst fragment, while approvals and generated/<id>/ use the
        trigger). An explicit "item" field is honored only when it agrees,
        so a reply can never claim another item's approval and files."""
        reply_to = item.get("reply_to")
        resolved = None
        if reply_to is not None:
            resolved = str(state.get("reply_aliases", {}).get(str(reply_to), reply_to))
        claimed = item.get("item")
        if claimed is None:
            return resolved
        if resolved is not None and str(claimed) != resolved:
            print(
                f"Note: outbox item names item {claimed} but replies to "
                f"{reply_to} (item {resolved}); no attachments",
                flush=True,
            )
            return None
        return str(claimed)

    def approval(self, state, item_id):
        """The live generation approval for a queue item ({"ts",
        "channel_id"}), or None."""
        a = state.get("gen_approved", {}).get(item_id) if item_id else None
        if not isinstance(a, dict) or self.now() - a.get("ts", 0) > generated_media.TTL_SECS:
            return None
        return a

    def new_attachments(self, state, item, gen_id):
        attachments, gen_files = [], []
        candidates = generated_media.validate_paths(self.directory, item.get("attachments"), gen_id)
        if not candidates:
            # The media tool usually returns a remote URL or handle, not
            # the local filename, so the worker can't name the file.
            found = generated_media.discover_generated(
                self.directory, state.get("gen_sent", []), gen_id
            )
            if found:
                print(f"Attaching discovered generated file: {found}", flush=True)
                candidates.append(found)
            else:
                print(
                    f"Note: approved generation for {gen_id} but no file in generated/{gen_id}/",
                    flush=True,
                )
        for rp in candidates:
            send_path, fname = generated_media.fit_for_discord(rp)
            if send_path:
                attachments.append((send_path, fname))
                gen_files.append([send_path, rp])
            else:
                print(f"Attachment too large even compressed: {rp}", flush=True)
        return attachments, gen_files

    def attachments(self, state, item, retry):
        """(gen_id, approval, attachments, gen_files) for a reply: only
        generated files bound to the queue item it answers
        (generated/<its message_id>/), under the monitor's live may_generate
        approval for that item (gen_approved; the outbox item does not carry
        the flag), and only into the channel the item came from. No approval,
        no files: generation is gated in code. attachments are (send path,
        filename) for the send; gen_files [send path, original] pairs, kept
        in _retry so a retry re-attaches the same files."""
        gen_id = self.item_id(state, item)
        approval = self.approval(state, gen_id)
        if approval and approval.get("channel_id") != item.get("channel_id"):
            print(
                f"Note: generation approval for item {gen_id} belongs to "
                f"channel {approval.get('channel_id')}, not "
                f"{item.get('channel_id')}; no attachments",
                flush=True,
            )
            approval = None
        attachments, gen_files = [], []
        if approval and "attachments" in retry:
            # A retry: the files the first attempt resolved. Re-checked,
            # because the outbox (and so _retry) is worker-writable.
            for pair in retry.get("attachments") or []:
                if (
                    isinstance(pair, list)
                    and len(pair) == GENERATED_PATH_PAIR_LENGTH
                    and len(generated_media.validate_paths(self.directory, pair, gen_id))
                    == GENERATED_PATH_PAIR_LENGTH
                ):
                    attachments.append((pair[0], os.path.basename(pair[0])))
                    gen_files.append(pair)
        elif approval:
            attachments, gen_files = self.new_attachments(state, item, gen_id)
        elif item.get("attachments"):
            print(
                f"Note: reply to {item.get('reply_to')} listed attachments "
                f"without a live generation approval for its item; dropped",
                flush=True,
            )
        return gen_id, approval, attachments, gen_files
