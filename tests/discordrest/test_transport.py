"""Real discord.py HTTP against a local server; no external Discord access."""

import concurrent.futures
import email.policy
import http.server
import json
import os
import sys
import threading
import time
import unittest
from email.parser import BytesParser

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_lib")
)
import common  # noqa: F401 (initialize paths and safe test environment)
from muse_discord.discord_rest import Rest, DiscordError, route_for


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def handle_request(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        with self.server.lock:
            self.server.requests.append(
                (self.command, self.path, dict(self.headers), body)
            )
            count = sum(p == self.path for _, p, _, _ in self.server.requests)
        path = self.path.split("?")[0]
        status, data, headers = 200, {"path": self.path}, {}
        if path == "/slow":
            time.sleep(0.15)
        if path == "/short-limit" and count == 1:
            status, data, headers = (
                429,
                {"retry_after": 0.02, "global": True},
                {"Via": "mock"},
            )
        if path == "/long-limit":
            status, data, headers = (
                429,
                {"retry_after": 2, "global": True},
                {"Via": "mock"},
            )
        if path == "/server-error" and count == 1:
            status, data = 500, {"message": "try again"}
        if (
            path == "/channels/123/messages"
            and count == 1
            and (self.path.endswith("?retry=1") or b'"retry"' in body)
        ):
            status, data = 500, {"message": "try again"}
        if path in ("/unauthorized", "/forbidden", "/missing"):
            status = {"/unauthorized": 401, "/forbidden": 403, "/missing": 404}[path]
            data = {"code": 50001, "message": "a secret that must not appear in errors"}
        if path.endswith("/callback") or path == "/typing":
            status, data = 204, None
        raw = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        if raw:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, val in headers.items():
            self.send_header(key, val)
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = handle_request


class Transport(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.requests, self.server.lock = [], threading.Lock()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.rest = Rest("test-token", f"http://127.0.0.1:{self.server.server_port}")

    def tearDown(self):
        self.rest.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_json_queries_and_bodyless_response_use_sdk_without_login_probe(self):
        self.assertEqual(
            self.rest.request("GET", "/channels/123/messages?limit=5&after=1")["path"],
            "/channels/123/messages?limit=5&after=1",
        )
        data = {
            "content": "hello",
            "nonce": "nonce-1",
            "enforce_nonce": True,
            "message_reference": {"message_id": "456", "fail_if_not_exists": False},
        }
        self.rest.request("POST", "/channels/123/messages", data)
        self.assertEqual(json.loads(self.server.requests[-1][3]), data)
        self.assertEqual(self.server.requests[-1][2]["Authorization"], "Bot test-token")
        self.assertIn("discord.py", self.server.requests[-1][2]["User-Agent"])
        self.assertEqual(self.rest.request("POST", "/typing"), "")
        self.assertFalse(any(p == "/users/@me" for _, p, _, _ in self.server.requests))

    def test_callback_is_anonymous_and_has_no_identity_request(self):
        anonymous = Rest(None, self.rest.base)
        try:
            self.assertEqual(
                anonymous.request(
                    "POST",
                    "/interactions/123/interaction-token/callback",
                    {"type": 7},
                    timeout=0.5,
                ),
                "",
            )
        finally:
            anonymous.close()
        self.assertEqual(len(self.server.requests), 1)
        self.assertNotIn("Authorization", self.server.requests[0][2])

    def test_bucket_keys_keep_major_parameters_and_escape_emoji(self):
        a, _ = route_for(
            "PUT", "/channels/123/messages/456/reactions/%F0%9F%91%8D%2F/@me"
        )
        b, _ = route_for("PUT", "/channels/123/messages/789/reactions/x/@me")
        self.assertEqual(a.key, b.key)
        self.assertEqual(a.major_parameters, "123")
        self.assertIn("%F0%9F%91%8D%2F", a.url)
        for path in ("https://evil.test/", "//evil.test/a"):
            with self.assertRaises(ValueError):
                route_for("GET", path)
        with self.assertRaises(ValueError):
            route_for("GET", "/users/@me", "http://evil.test/api")

    def test_sdk_handles_short_global_limits_and_server_retries(self):
        self.rest.request("GET", "/short-limit")
        self.assertEqual(
            sum(p == "/short-limit" for _, p, _, _ in self.server.requests), 2
        )
        self.rest.request("GET", "/server-error", timeout=3)
        self.assertEqual(
            sum(p == "/server-error" for _, p, _, _ in self.server.requests), 2
        )
        self.rest.request("GET", "/after-limit")

    def test_long_limits_return_to_the_caller_without_blocking(self):
        start = time.monotonic()
        with self.assertRaises(DiscordError) as cm:
            self.rest.request("GET", "/long-limit")
        self.assertEqual(cm.exception.code, 429)
        self.assertEqual(float(cm.exception.headers["Retry-After"]), 2)
        self.assertLess(time.monotonic() - start, 0.5)
        self.rest.request("GET", "/after-limit")

    def test_errors_preserve_http_status_without_disclosing_response_body(self):
        for path, status in [
            ("/unauthorized", 401),
            ("/forbidden", 403),
            ("/missing", 404),
        ]:
            with self.assertRaises(DiscordError) as cm:
                self.rest.request("GET", path)
            self.assertEqual(cm.exception.code, status)
            self.assertEqual(cm.exception.discord_code, 50001)
            self.assertNotIn("secret", str(cm.exception))

    def test_timed_out_session_recovers_and_does_not_send_late_requests(self):
        with self.assertRaises(TimeoutError):
            self.rest.request("GET", "/slow", timeout=0.03)
        self.assertEqual(
            self.rest.request("GET", "/after-timeout")["path"], "/after-timeout"
        )
        time.sleep(0.2)
        self.assertEqual(sum(p == "/slow" for _, p, _, _ in self.server.requests), 1)

    def test_sdk_multipart_preserves_payload_and_exact_file_bytes(self):
        for payload in [
            {"content": "hi", "nonce": "n", "enforce_nonce": True},
            {"name": "forum", "message": {"content": "starter"}},
        ]:
            self.rest.request(
                "POST",
                "/channels/123/threads"
                if "message" in payload
                else "/channels/123/messages?retry=1",
                payload,
                files=[("cat.png", b"\x00\xffimage")],
            )
            _, _, headers, body = self.server.requests[-1]
            msg = BytesParser(policy=email.policy.default).parsebytes(
                ("Content-Type: " + headers["Content-Type"] + "\r\n\r\n").encode()
                + body
            )
            parts = {
                p.get_param("name", header="content-disposition"): p
                for p in msg.iter_parts()
            }
            actual = json.loads(parts["payload_json"].get_payload(decode=True))
            target = actual.get("message", actual)
            self.assertEqual(target.pop("attachments")[0]["filename"], "cat.png")
            self.assertEqual(actual, payload)
            self.assertEqual(
                parts["files[0]"].get_payload(decode=True), b"\x00\xffimage"
            )

    def test_concurrent_sync_callers_share_one_async_client(self):
        with concurrent.futures.ThreadPoolExecutor(4) as pool:
            result = list(
                pool.map(lambda i: self.rest.request("GET", f"/parallel/{i}"), range(8))
            )
        self.assertEqual(len(result), 8)
        self.assertEqual(len(self.server.requests), 8)

    def test_message_retries_have_a_stable_nonce_but_creates_are_not_retried(self):
        with self.assertRaises(DiscordError):
            self.rest.request("POST", "/server-error", {"name": "thread"})
        self.assertEqual(len(self.server.requests), 1)
        self.rest.request(
            "POST", "/channels/123/messages", {"content": "retry"}, timeout=3
        )
        sent = [
            json.loads(body)
            for _, path, _, body in self.server.requests
            if path == "/channels/123/messages"
        ]
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], sent[1])
        payload = sent[-1]
        self.assertTrue(payload["nonce"])
        self.assertTrue(payload["enforce_nonce"])


if __name__ == "__main__":
    unittest.main()
