"""Message pipelines work without importing or initializing the monitor."""

import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.ingest import IngestConversation, IngestJournal, IngestPolicy, MessageEnqueuer
from muse_discord.message_models import QueueContext, SecretBlocked
from muse_discord.muse_settings import Scope, Settings
from muse_discord.reply_delivery import SEND_RETRY_SECS, OutboxDelivery, ReplyEffects, ReplyPolicy


class Ingress(unittest.TestCase):
    def setUp(self):
        self.msg = {
            "id": "100",
            "channel_id": "200",
            "content": "hello",
            "author": {"id": "300", "username": "owner"},
        }
        self.policy = Mock(spec=IngestPolicy)
        self.policy.owners = {"300"}
        self.policy.predates_join.return_value = False
        self.policy.is_bot_author.return_value = False
        self.policy.scope_for.return_value = Scope.dm()
        self.here = Settings().resolve(Scope.dm())
        self.policy.settings.return_value.resolve.return_value = self.here
        self.policy.rate_block.return_value = None
        self.policy.media_permission.return_value = False
        self.conversation = Mock(spec=IngestConversation)
        self.conversation.collect_media.return_value = ([], [], [])
        self.conversation.get_reply_context.return_value = None
        self.conversation.bot_run_length.return_value = 0
        self.conversation.conversation_participants.return_value = []
        self.conversation.settings_card.return_value = {"speaker": "owner", "where": "dm"}
        self.conversation.watch_card.return_value = {}
        self.conversation.is_forwarded.return_value = False
        self.journal = Mock(spec=IngestJournal)
        self.journal.is_suspicious.return_value = False
        self.journal.note_queued.return_value = 123.0
        self.queue = []
        self.pipeline = MessageEnqueuer(
            self.msg,
            "owner DM",
            {},
            self.queue,
            QueueContext(dm=True),
            self.policy,
            self.conversation,
            self.journal,
        )

    def test_owner_origin_reaches_capability_and_metadata_recording(self):
        self.assertTrue(self.pipeline.enqueue())
        self.assertEqual(self.queue[0]["author_id"], "300")
        self.assertEqual(self.queue[0]["kind"], "dm")
        self.assertEqual(self.queue[0]["queued_at"], 123.0)
        meta = self.journal.note_item_meta.call_args.kwargs
        self.assertTrue(meta["owner_dm"])
        self.assertEqual(meta["author_id"], "300")
        self.assertFalse(meta["forwarded"])
        self.journal.owner_dm_mint_nonce.assert_called_once_with({}, "100", "200", [])
        self.journal.reply_nonce_mint.assert_called_once_with({}, "100")

    def test_suspicious_document_cannot_reach_queue_or_capability_issuance(self):
        self.here["privacy.suspicious"] = "hold-for-review"
        self.conversation.collect_media.return_value = ([], [{"name": "x", "text": "attack"}], [])
        self.journal.is_suspicious.side_effect = [False, "injection"]
        self.assertFalse(self.pipeline.enqueue())
        self.assertEqual(self.queue, [])
        self.journal.quarantine.assert_called_once()
        self.journal.reply_nonce_mint.assert_not_called()
        self.journal.owner_dm_mint_nonce.assert_not_called()

    def test_daily_cap_prevents_media_io_and_capability_issuance(self):
        self.policy.rate_block.return_value = {"key": "limits.per_day", "cap": 2}
        self.assertFalse(self.pipeline.enqueue())
        self.assertEqual(self.queue, [])
        self.conversation.collect_media.assert_not_called()
        self.policy.rate_record.assert_not_called()
        self.journal.reply_nonce_mint.assert_not_called()


class Delivery(unittest.TestCase):
    def setUp(self):
        self.item = {"item": "100", "reply_to": "100", "channel_id": "200", "text": "hello"}
        self.batch = SimpleNamespace(
            state={"replied_to": []},
            picked=time.time(),
            index={},
            remaining=[],
            rejects=[],
            settled=[],
        )
        self.policy = Mock(spec=ReplyPolicy)
        self.policy._outbox_problem.return_value = None
        self.policy.bind_started.return_value = (None, False)
        self.policy.owner_dm_gate.return_value = None
        self.policy.own_channel_problem.return_value = None
        self.policy.bound_elsewhere.return_value = False
        self.policy._answered_id.return_value = "100"
        self.policy.never_share_hit.return_value = False
        self.policy._reply_attachments.return_value = (None, None, [], [])
        self.effects = Mock(spec=ReplyEffects)
        self.effects.opt_action.return_value = None
        self.effects.remember_check.return_value = (None, None)
        self.effects.normalize_parts.return_value = ["hello"]
        self.effects.link_mentions.side_effect = lambda text, *_: text
        self.pipeline = OutboxDelivery(self.batch, self.item, self.policy, self.effects, {})

    def test_owner_dm_refusal_precedes_actions_and_settlement(self):
        self.policy.owner_dm_gate.return_value = "wrong owner nonce"
        self.pipeline.process()
        self.policy._refuse.assert_called_once()
        self.assertEqual(self.effects.mock_calls, [])
        self.assertEqual(self.batch.settled, [])

    def test_expired_retry_never_calls_transport(self):
        self.item["_retry"] = {"first": time.time() - SEND_RETRY_SECS - 1}
        self.pipeline.process()
        self.effects.send_fragmented.assert_not_called()
        self.effects._settle.assert_called_once()
        self.assertEqual(self.batch.settled, [self.item])

    def test_blocked_secret_settles_without_retry_or_memory_write(self):
        self.effects.send_fragmented.side_effect = SecretBlocked("secret")
        self.pipeline.process()
        outcome = self.effects._settle.call_args.kwargs["outcome"]
        self.assertEqual(outcome.status, "skipped")
        self.assertEqual(self.batch.remaining, [])
        self.assertEqual(self.batch.settled, [self.item])
        self.effects.save_facts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
