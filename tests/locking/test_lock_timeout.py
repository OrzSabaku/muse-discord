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

from muse_discord.message_models import MessagePlace
from muse_discord.outbox_store import OUTBOX_POISON_TRIES
from muse_discord.reply_delivery import SEND_RETRY_SECS
from muse_discord.storage import SharedLockTimeout


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
        m._OUTBOX.poison.clear()

    def test_queue_is_unchanged_until_lock_released_and_retry_preserves_worker_append(self):
        first, worker, incoming = [{"message_id": snowflake()} for _ in range(3)]
        write(m.QUEUE_FILE, [first])
        with held_lock():
            with self.assertRaises(SharedLockTimeout):
                m._queue_files().edit(add=[incoming], drop=lambda q: q == first)
            self.assertEqual(read(m.QUEUE_FILE), [first])
            write(m.QUEUE_FILE, [first, worker])  # the lock holder's write
        self.assertEqual(m._queue_files().edit(add=[incoming], drop=lambda q: q == first), 1)
        self.assertEqual(read(m.QUEUE_FILE), [worker, incoming])

    def test_non_contention_lock_error_is_propagated_without_entering_body(self):
        with (
            patch.object(fcntl, "flock", side_effect=OSError(errno.EIO, "lock I/O error")),
            self.assertRaises(OSError) as raised,
            m._queue_files().lock(),
        ):
            self.fail("the writer entered without acquiring the lock")
        self.assertEqual(raised.exception.errno, errno.EIO)

    def test_outbox_take_is_deferred_without_poisoning_or_modifying_files(self):
        state = new_state()
        item = {"item": snowflake(), "channel_id": "101", "text": "waiting"}
        write(m.OUTBOX_FILE, [item])
        with held_lock(), self.assertRaises(SharedLockTimeout):
            m._OUTBOX.pass_once(state)
        self.assertEqual(read(m.OUTBOX_FILE), [item])
        self.assertFalse(os.path.exists(m.OUTBOX_PROCESSING_FILE))
        self.assertEqual(m._OUTBOX.poison, {})
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
                self.assertRaises(SharedLockTimeout),
            ):
                m._OUTBOX.pass_once(state)
            self.assertEqual(state["reply_nonces"][mid]["nonce"], nonce)
            self.assertEqual(len(read(m.QUEUE_FILE)), 1)
            self.assertEqual(read(m.OUTBOX_PROCESSING_FILE), [item])
        with capture():
            m._OUTBOX.pass_once(state)
        self.assertEqual(sent_texts(), ["hi"])
        self.assertEqual(read(m.QUEUE_FILE), [])
        self.assertNotIn(mid, state["reply_nonces"])
        self.assertFalse(os.path.exists(m.OUTBOX_PROCESSING_FILE))
        self.assertEqual(m._OUTBOX.poison, {})

    def test_retry_progress_survives_repeated_settlement_contention_and_reload(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        write(m.OUTBOX_FILE, [item])
        clock = [m.time.time()]
        first = clock[0]
        with open(m.LOCK_FILE, "a") as holder, patch.object(m.time, "time", lambda: clock[0]):

            def fail_send(*_args, **_kwargs):
                fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
                raise OSError("ambiguous send failure")

            with patch.object(m, "api_post", side_effect=fail_send) as send, capture():
                for attempt in (1, 2):
                    with self.assertRaises(SharedLockTimeout):
                        m._OUTBOX.pass_once(state)
                    checkpoint = read(m.STATE_FILE)
                    retry = next(iter(checkpoint["outbox_retries"].values()))
                    self.assertEqual(retry["n"], attempt)
                    self.assertEqual(retry["first"], first)
                    self.assertGreater(retry["next"], clock[0])
                    # The monitor's checkpoint is the only updated file while locked.
                    processing_retry = read(m.OUTBOX_PROCESSING_FILE)[0].get("_retry", {})
                    self.assertEqual(processing_retry.get("n", 0), attempt - 1)
                    self.assertEqual(send.call_count, attempt)
                    state = checkpoint  # restart with progress loaded from state.json
                    fcntl.flock(holder, fcntl.LOCK_UN)
                    m._OUTBOX.pass_once(state)  # still before the saved backoff deadline
                    self.assertEqual(send.call_count, attempt)
                    self.assertEqual(read(m.OUTBOX_FILE)[0]["_retry"], retry)
                    self.assertNotIn("outbox_retries", read(m.STATE_FILE))
                    clock[0] = retry["next"] + 0.1
                clock[0] = first + SEND_RETRY_SECS + 1
                m._OUTBOX.pass_once(state)
                self.assertEqual(send.call_count, 2)  # expired replies are never sent again
        self.assertEqual(read(m.OUTBOX_FILE), [])
        self.assertEqual(read(m.QUEUE_FILE), [])
        self.assertEqual(read(m.LEDGER_FILE)["entries"][mid]["status"], "skipped")

    def test_nonce_publication_runs_after_releasing_queue_lock(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        write(m.OUTBOX_FILE, [item])
        original_publish = m._CAPABILITIES.write_replies

        def publish(current):
            with held_lock():
                pass  # another writer can acquire the lock while publication runs
            return original_publish(current)

        with patch.object(m._CAPABILITIES, "write_replies", publish), capture():
            m._OUTBOX.pass_once(state)
        self.assertEqual(sent_texts(), ["hi"])
        self.assertEqual(read(m.QUEUE_FILE), [])

    def test_staged_addition_survives_timeout_and_restart_then_next_tick_publishes_it(self):
        state, incoming = new_state(), msg()
        write(m.QUEUE_FILE, [])
        with (
            patch.object(
                m._SETTINGS, "engine", m._SETTINGS.engine.model_copy(update={"coalesce_seconds": 0})
            ),
            capture(),
        ):
            m.stage_trigger(state, incoming, "general", "mention", place=MessagePlace("G"))
            with held_lock(), self.assertRaises(SharedLockTimeout):
                m._QUEUE_STAGING.stage(state, [])
        restored = read(m.STATE_FILE)
        self.assertFalse(restored["pending"])
        self.assertEqual(restored["queue_backlog"][0]["message_id"], incoming["id"])
        self.assertEqual(read(m.QUEUE_FILE), [])
        with capture():
            m._QUEUE_STAGING.stage(restored, [])
        self.assertEqual([q["message_id"] for q in read(m.QUEUE_FILE)], [incoming["id"]])
        self.assertNotIn("queue_backlog", read(m.STATE_FILE))

    def test_contention_during_item_handling_does_not_mark_it_as_poison(self):
        state, mid = new_state(), snowflake()
        write(m.QUEUE_FILE, [{"message_id": mid, "channel_id": "101"}])
        item = signed(m, state, {"item": mid, "reply_to": mid, "channel_id": "101", "text": "hi"})
        write(m.OUTBOX_FILE, [item])
        with patch.object(m.OutboxDelivery, "actions", side_effect=SharedLockTimeout("busy")):
            for _ in range(OUTBOX_POISON_TRIES):
                with self.assertRaises(SharedLockTimeout):
                    m._OUTBOX.pass_once(state)
        self.assertEqual(m._OUTBOX.poison, {})
        self.assertEqual(read(m.OUTBOX_PROCESSING_FILE), [item])
        self.assertFalse(os.path.exists(m.OUTBOX_REJECTED_FILE))
        with capture():
            m._OUTBOX.pass_once(state)
        self.assertEqual(sent_texts(), ["hi"])

    def test_startup_continues_with_pending_burst_when_queue_is_locked(self):
        state, incoming = new_state(), msg()
        with capture():
            m.stage_trigger(state, incoming, "general", "mention", place=MessagePlace("G"))
        inbox, status = object(), {"connected": False}
        with (
            patch.object(m._PROCESS_LOCK, "acquire"),
            patch.object(m.TM, "start"),
            patch.object(m, "api_get", return_value={"id": "999", "username": "pip"}),
            patch.object(m, "discover_channels", return_value=({}, {})),
            patch.object(m, "publish_directory"),
            patch.object(m, "load_state", return_value=state),
            patch.object(m._GUILD_LIFECYCLE, "sync"),
            patch.object(m._DIRECTORY, "channels", {}),
            patch.object(m._DIRECTORY, "guilds", {}),
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
            m._QUEUE_STAGING.stage(restored, [])
        self.assertEqual([q["message_id"] for q in read(m.QUEUE_FILE)], [incoming["id"]])

    def test_replayed_backlog_does_not_resurrect_a_settled_item(self):
        for status in ("replied", "skipped"):
            with self.subTest(status=status):
                state, mid = new_state(), snowflake()
                state["queue_backlog"] = [{"message_id": mid, "channel_id": "101"}]
                write(m.QUEUE_FILE, [])
                write(m.LEDGER_FILE, {"entries": {mid: {"status": status}}})
                m._queue_publication().publish(state)
                self.assertEqual(read(m.QUEUE_FILE), [])
                self.assertNotIn("queue_backlog", state)


if __name__ == "__main__":
    unittest.main(argv=ARGV)
