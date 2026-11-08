"""Lock contention must defer writes without losing staged or delivered replies."""

import errno
import fcntl
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
from common import signed
from harness import ARGV, capture, fresh_pkg, msg, new_state, read, sent_texts, snowflake, write
from harness import monitor as m


@contextmanager
def held_lock():
    with open(m.LOCK_FILE, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


class LockTimeout(unittest.TestCase):
    def setUp(self):
        fresh_pkg()
        self.timeout = patch.object(m, "LOCK_WAIT_SECS", 0)
        self.timeout.start()
        self.addCleanup(self.timeout.stop)
        m._OUTBOX_POISON.clear()

    def test_queue_is_unchanged_until_lock_released_and_retry_preserves_worker_append(self):
        first, worker, incoming = [{"message_id": snowflake()} for _ in range(3)]
        write(m.QUEUE_FILE, [first])
        with held_lock():
            with self.assertRaises(m.SharedLockTimeout):
                m.queue_edit(add=[incoming], drop=lambda q: q == first)
            self.assertEqual(read(m.QUEUE_FILE), [first])
            write(m.QUEUE_FILE, [first, worker])  # the lock holder's write
        self.assertEqual(m.queue_edit(add=[incoming], drop=lambda q: q == first), 1)
        self.assertEqual(read(m.QUEUE_FILE), [worker, incoming])

    def test_non_contention_lock_error_is_propagated_without_entering_body(self):
        with (
            patch.object(m.fcntl, "flock", side_effect=OSError(errno.EIO, "lock I/O error")),
            self.assertRaises(OSError) as raised,
            m.shared_lock(),
        ):
            self.fail("the writer entered without acquiring the lock")
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_outbox_take_is_deferred_without_poisoning_or_modifying_files(self):
        state = new_state()
        item = {"item": snowflake(), "channel_id": "101", "text": "waiting"}
        write(m.OUTBOX_FILE, [item])
        with held_lock(), self.assertRaises(m.SharedLockTimeout):
            m.outbox_pass(state)
        self.assertEqual(read(m.OUTBOX_FILE), [item])
        self.assertFalse(os.path.exists(m.OUTBOX_PROCESSING_FILE))
        self.assertEqual(m._OUTBOX_POISON, {})
        self.assertEqual(sent_texts(), [])

    def test_failed_settlement_keeps_nonce_and_retry_does_not_send_twice(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        nonce = state["reply_nonces"][mid]["nonce"]
        write(m.OUTBOX_FILE, [item])
        original_post = m.api_post
        with open(m.LOCK_FILE, "a") as holder:

            def post(path, data):
                result = original_post(path, data)
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return result

            with (
                patch.object(m, "api_post", post),
                capture(),
                self.assertRaises(m.SharedLockTimeout),
            ):
                m.outbox_pass(state)
            self.assertEqual(state["reply_nonces"][mid]["nonce"], nonce)
            self.assertEqual(len(read(m.QUEUE_FILE)), 1)
            self.assertEqual(read(m.OUTBOX_PROCESSING_FILE), [item])
        with capture():
            m.outbox_pass(state)
        self.assertEqual(sent_texts(), ["hi"])
        self.assertEqual(read(m.QUEUE_FILE), [])
        self.assertNotIn(mid, state["reply_nonces"])
        self.assertFalse(os.path.exists(m.OUTBOX_PROCESSING_FILE))
        self.assertEqual(m._OUTBOX_POISON, {})

    def test_nonce_publication_runs_after_releasing_queue_lock(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        write(m.OUTBOX_FILE, [item])
        original_publish = m.reply_nonces_write

        def publish(current):
            with held_lock():
                pass  # another writer can acquire the lock while publication runs
            return original_publish(current)

        with patch.object(m, "reply_nonces_write", publish), capture():
            m.outbox_pass(state)
        self.assertEqual(sent_texts(), ["hi"])
        self.assertEqual(read(m.QUEUE_FILE), [])

    def test_staged_addition_survives_timeout_and_restart_then_next_tick_publishes_it(self):
        state, incoming = new_state(), msg()
        write(m.QUEUE_FILE, [])
        with patch.object(m, "COALESCE_SECS", 0), capture():
            m.stage_trigger(state, incoming, "general", "mention", place=m.MessagePlace("G"))
            with held_lock(), self.assertRaises(m.SharedLockTimeout):
                m._stage_polled_messages(state, [])
        restored = read(m.STATE_FILE)
        self.assertFalse(restored["pending"])
        self.assertEqual(restored["queue_backlog"][0]["message_id"], incoming["id"])
        self.assertEqual(read(m.QUEUE_FILE), [])
        with capture():
            m._stage_polled_messages(restored, [])
        self.assertEqual([q["message_id"] for q in read(m.QUEUE_FILE)], [incoming["id"]])
        self.assertNotIn("queue_backlog", read(m.STATE_FILE))

    def test_contention_during_item_handling_does_not_mark_it_as_poison(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        write(m.OUTBOX_FILE, [item])
        with patch.object(m.OutboxDelivery, "actions", side_effect=m.SharedLockTimeout("busy")):
            for _ in range(m.OUTBOX_POISON_TRIES):
                with self.assertRaises(m.SharedLockTimeout):
                    m.outbox_pass(state)
        self.assertEqual(m._OUTBOX_POISON, {})
        self.assertEqual(read(m.OUTBOX_PROCESSING_FILE), [item])
        self.assertFalse(os.path.exists(m.OUTBOX_REJECTED_FILE))
        with capture():
            m.outbox_pass(state)
        self.assertEqual(sent_texts(), ["hi"])

    def test_startup_continues_with_pending_burst_when_queue_is_locked(self):
        state, incoming = new_state(), msg()
        with capture():
            m.stage_trigger(state, incoming, "general", "mention", place=m.MessagePlace("G"))
        inbox, status = object(), {"connected": False}
        with (
            patch.object(m, "_acquire_singleton"),
            patch.object(m.TM, "start"),
            patch.object(m, "api_get", return_value={"id": "999", "username": "pip"}),
            patch.object(m, "discover_channels", return_value=({}, {})),
            patch.object(m, "publish_directory"),
            patch.object(m, "load_state", return_value=state),
            patch.object(m, "sync_guilds"),
            patch.object(m, "CHANNELS", {}),
            patch.object(m, "GUILDS", {}),
            patch.object(m, "_start_monitor_threads", return_value=(inbox, status)),
            held_lock(),
            capture(),
        ):
            restored, actual_inbox, actual_status = m._start_monitor()
        self.assertIs(restored, state)
        self.assertIs(actual_inbox, inbox)
        self.assertIs(actual_status, status)
        self.assertEqual(read(m.STATE_FILE)["queue_backlog"][0]["message_id"], incoming["id"])
        with capture():
            m._stage_polled_messages(restored, [])
        self.assertEqual([q["message_id"] for q in read(m.QUEUE_FILE)], [incoming["id"]])

    def test_replayed_backlog_does_not_resurrect_a_settled_item(self):
        for status in ("replied", "skipped"):
            with self.subTest(status=status):
                state, mid = new_state(), snowflake()
                state["queue_backlog"] = [{"message_id": mid, "channel_id": "101"}]
                write(m.QUEUE_FILE, [])
                write(m.LEDGER_FILE, {"entries": {mid: {"status": status}}})
                m.publish_queue_additions(state)
                self.assertEqual(read(m.QUEUE_FILE), [])
                self.assertNotIn("queue_backlog", state)


if __name__ == "__main__":
    unittest.main(argv=ARGV)
