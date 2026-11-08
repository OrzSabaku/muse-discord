"""Backoff and credential failures above a fake SDK transport, without a monitor."""

import io
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "_lib"))
import common  # noqa: F401 (select the source tree under test)

from muse_discord.http_runtime import DiscordCalls, RequestOptions, RestBackoff
from muse_discord.monitor_health import MonitorHealth


def limited(headers=None, body=b"{}"):
    return HTTPError("unused", 429, "limited", headers or {}, io.BytesIO(body))


class RequestPolicy(unittest.TestCase):
    def test_explicit_retry_delays_do_not_shorten_an_existing_window(self):
        clock = [100.0]
        backoff = RestBackoff(2, 10, lambda: clock[0], lambda low, high: 0)
        backoff.note(limited({"Retry-After": "30"}))
        clock[0] = 110
        backoff.note(limited({"Retry-After": "1"}))
        self.assertEqual(backoff.until, 130)
        clock[0] = 140
        backoff.note(limited({"Retry-After": "bad"}, b'{"retry_after": 12}'))
        self.assertEqual(backoff.until, 152)
        clock[0] = 153
        backoff.note(limited())
        self.assertEqual(backoff.until, 155)
        backoff.clean_cycle()
        self.assertEqual(backoff.step, 1)  # a still-blocked cycle is not clean
        clock[0] = 156
        backoff.clean_cycle()
        self.assertEqual(backoff.step, 0)

    def test_anonymous_callback_failure_does_not_reject_the_bot_credential(self):
        health = MonitorHealth(lambda: "unused")
        health.progress = Mock()
        calls = DiscordCalls(lambda: "credential", RestBackoff(2, 10), health, lambda data, _: data)
        error = HTTPError("unused", 401, "unauthorized", {}, None)
        sdk = Mock()
        sdk.request.side_effect = error
        with patch("muse_discord.discord_rest.transport", return_value=sdk) as transport:
            with self.assertRaises(HTTPError):
                calls.request("POST", "/callback", {}, RequestOptions(anonymous=True))
            self.assertIsNone(health.rejected)
            self.assertIsNone(transport.call_args.args[0])
            with self.assertRaises(HTTPError) as raised:
                calls.get("/users/@me")
            self.assertIs(raised.exception, error)
            self.assertEqual(health.rejected, "401 from the REST API")
            self.assertEqual(transport.call_args.args[0], "credential")
        health.progress.assert_called_once()

    def test_failed_rest_call_still_records_watchdog_progress(self):
        with tempfile.TemporaryDirectory() as path:
            heartbeat = os.path.join(path, "monitor.heartbeat")
            health = MonitorHealth(lambda: heartbeat, lambda: 100.0)
            calls = DiscordCalls(
                lambda: "credential", RestBackoff(2, 10), health, lambda data, _: data
            )
            error = OSError("temporary network failure")
            operation = Mock(side_effect=error)
            with self.assertRaises(OSError) as raised:
                calls.call(operation)
            self.assertIs(raised.exception, error)
            self.assertTrue(os.path.exists(heartbeat))
            self.assertIsNone(health.rejected)


if __name__ == "__main__":
    unittest.main()
